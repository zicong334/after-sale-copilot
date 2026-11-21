"""R4/R5(2026-11-21 企业工程评审):限流迁 Redis —— 集群口径 + 键自动过期。

原判据命中两条反模式:
- **本地计数器限流**:`MemoryRateLimiter` 是进程内的,N 个实例集群实际放行 = N × 阈值;
- **本地缓存无界**:`_buckets` 按 subject 只增不清,长跑 + 大量主体持续涨内存。

⚠️⚠️ **验证边界(必须读)** ⚠️⚠️
本机**无 redis-server**。本文件只断言**我自己写的逻辑**:
键命名空间、单次 EVAL(原子性意图)、脚本里带 TTL、后端选择、
Redis 故障时的降级与 fail-open/fail-close 分档。

**下列结论本机测不到,一律标记「未运行时验证」**(见 docs/待验清单_20261121.md):
- 集群总放行量 ≈ 阈值而非 N×(要两个真进程 + 真 Redis);
- `EXPIRE` 是否真的让无界增长消失(要真 Redis 的 TTL 行为);
- **Lua 的真原子性**(单进程模拟证明不了并发原子)。
**不得**用"我调用了 Redis 客户端"的断言冒充上述结论。
"""
from __future__ import annotations

import pytest

from app.ratelimit_redis import (
    REFUND_LUA,
    TOKEN_BUCKET_LUA,
    RedisRateLimiter,
    build_limiters,
    fail_mode_for_path,
)
from app.security import MemoryRateLimiter


class _RecordingClient:
    """记录调用参数的假客户端。**只用来验我的逻辑,不代表 Redis 语义**。"""

    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self.result = result if result is not None else [1, 0]

    def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        return self.result


class _BrokenClient:
    def eval(self, *a, **k):
        raise ConnectionError("redis down")


# --- 键命名空间 -----------------------------------------------------------

def test_user_and_tenant_keys_do_not_collide():
    c = _RecordingClient()
    user = RedisRateLimiter(5, 10, client=c, prefix="rl:user")
    tenant = RedisRateLimiter(50, 50, client=c, prefix="rl:tenant")
    assert user.key_for("alice") != tenant.key_for("alice")
    assert user.key_for("alice").startswith("rl:user:")
    assert tenant.key_for("alice").startswith("rl:tenant:")


# --- 脚本:单轮次 + 带 TTL -------------------------------------------------

def test_token_bucket_script_sets_ttl():
    """无界增长的解法:键必须带过期。**这是 R5 的修法核心**。"""
    assert "EXPIRE" in TOKEN_BUCKET_LUA.upper() or "PEXPIRE" in TOKEN_BUCKET_LUA.upper()


def test_refund_script_sets_ttl_and_caps_at_burst():
    up = REFUND_LUA.upper()
    assert "EXPIRE" in up or "PEXPIRE" in up
    assert "MIN" in up or "math.min" in REFUND_LUA, "补回令牌不得超过桶容量"


def test_consume_is_a_single_round_trip():
    """一次 EVAL 完成"补充+扣减+过期",不做 read-modify-write 多次往返。"""
    c = _RecordingClient()
    lim = RedisRateLimiter(5, 10, client=c, prefix="rl:user")
    lim.consume("alice", now=1000.0)
    assert len(c.calls) == 1, f"应恰好一次 EVAL,实际 {len(c.calls)}"
    assert c.calls[0][1] == 1, "numkeys 应为 1"


def test_refund_is_a_single_round_trip():
    c = _RecordingClient()
    lim = RedisRateLimiter(5, 10, client=c, prefix="rl:user")
    lim.refund("alice")
    assert len(c.calls) == 1


def test_consume_parses_allowed_and_retry_after():
    c = _RecordingClient(result=[0, 2])
    lim = RedisRateLimiter(5, 10, client=c, prefix="rl:user")
    allowed, retry = lim.consume("alice", now=1000.0)
    assert allowed is False
    assert retry == 2.0


# --- 故障降级与分档 -------------------------------------------------------

def test_redis_down_fail_open_allows():
    lim = RedisRateLimiter(5, 10, client=_BrokenClient(), prefix="rl:user", fail_mode="open")
    allowed, _ = lim.consume("alice", now=1000.0)
    assert allowed is True, "fail-open 下 Redis 挂了应放行(纯读问答可用性优先)"


def test_redis_down_fail_close_rejects():
    lim = RedisRateLimiter(5, 10, client=_BrokenClient(), prefix="rl:user", fail_mode="closed")
    allowed, retry = lim.consume("alice", now=1000.0)
    assert allowed is False, "fail-close 下 Redis 挂了应拒绝(资损链路保守)"
    assert retry > 0


def test_per_call_fail_mode_overrides_instance_default():
    lim = RedisRateLimiter(5, 10, client=_BrokenClient(), prefix="rl:user", fail_mode="open")
    allowed, _ = lim.consume("alice", now=1000.0, fail_mode="closed")
    assert allowed is False


def test_refund_never_raises_when_redis_down():
    """补回令牌是辅助动作,Redis 挂了不能把主链路带崩。"""
    lim = RedisRateLimiter(5, 10, client=_BrokenClient(), prefix="rl:user")
    lim.refund("alice")  # 不抛即通过


@pytest.mark.parametrize("path,expected", [
    ("/refunds", "closed"),
    ("/refunds/abc/approve", "closed"),
    ("/orders/abc/pay", "closed"),
    ("/tickets", "closed"),
    ("/api/c/chat", "closed"),
    ("/api/c/sessions/s1/messages", "open"),
    ("/api/kb/items", "open"),
    ("/auth/me", "open"),
])
def test_fail_mode_is_classified_by_path(path, expected):
    """资损/写链路 fail-close,纯读 fail-open —— 不能一刀切。"""
    assert fail_mode_for_path(path) == expected


# --- 后端选择:默认路径必须一字不变 ---------------------------------------

def test_no_redis_url_keeps_memory_backend(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    user, tenant = build_limiters()
    assert isinstance(user, MemoryRateLimiter)
    assert isinstance(tenant, MemoryRateLimiter)


def test_redis_url_selects_redis_backend(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    user, tenant = build_limiters(client_factory=lambda url: _RecordingClient())
    assert isinstance(user, RedisRateLimiter)
    assert isinstance(tenant, RedisRateLimiter)


def test_client_factory_failure_falls_back_to_memory(monkeypatch):
    """连不上 Redis **不能让服务起不来**:降级回进程内并告警。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")

    def _boom(url):
        raise ConnectionError("no redis")

    user, tenant = build_limiters(client_factory=_boom)
    assert isinstance(user, MemoryRateLimiter)
    assert isinstance(tenant, MemoryRateLimiter)