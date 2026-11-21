"""R8(2026-11-21 企业工程评审):语义缓存加 L2 + 失效广播。

原判据:缓存只有**进程内 L1**(512 条 LRU、TTL 30min),多实例时各自一份,
一致性窗口 = TTL —— 政策改了各实例不同时生效。

设计口径(**刻意不做"把 TF-IDF 矩阵搬进 Redis"**)
------------------------------------------------
- **L1 仍负责语义相似匹配**(改述命中);
- **L2 只做归一化精确键命中**。理由:把相似度矩阵与候选语料塞进 Redis 既贵又难维护,
  而 L2 的价值主要是"跨实例复用已算过的答案",精确键足够。
  ⚠️ 这意味着 **L2 不命中改述**,这是有意的取舍,不是缺陷。
- 失效走 **按租户分频道的 Pub/Sub**,消息体只带 key(不带正文,避免把回复内容广播出去)。

⚠️⚠️ 验证边界 ⚠️⚠️
本机**无 redis-server**。本文件只断言**自写逻辑**:键/频道命名、L1↔L2 交互、
写入与失效的调用形态、Redis 故障降级、**默认关闭时 L1 路径一字不变**。
**未运行时验证**:跨实例命中率、Pub/Sub 真实失效延迟、L1/L2 版本差。
不得用假客户端的断言冒充这些结论(见 docs/待验清单_20261121.md)。
"""
from __future__ import annotations

import time

import pytest

from app.cache import CacheEntry, SemanticCache
from app.cache_l2 import RedisL2Cache, build_l2


class _FakeRedis:
    """记录调用的假客户端。**只验我的逻辑,不代表 Redis 语义**。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.ttls.pop(k, None)

    def publish(self, channel, message):
        self.published.append((channel, message))
        return 1


class _BrokenRedis:
    def get(self, *a, **k):
        raise ConnectionError("redis down")

    def set(self, *a, **k):
        raise ConnectionError("redis down")

    def delete(self, *a, **k):
        raise ConnectionError("redis down")

    def publish(self, *a, **k):
        raise ConnectionError("redis down")


def _payload(reply="退货期限是 7 天。"):
    return {"reply": reply, "citations": [], "intent": "policy_qa",
            "confidence": 0.9, "intent_reason": "命中退货时效"}


# --- 键与频道命名 ---------------------------------------------------------

def test_l2_key_is_tenant_scoped():
    c = RedisL2Cache(client=_FakeRedis())
    a = c.key_for("m1", "退货运费谁承担")
    b = c.key_for("m2", "退货运费谁承担")
    assert a != b, "跨租户键必须不同,否则串读"


def test_invalidate_channel_is_tenant_scoped():
    c = RedisL2Cache(client=_FakeRedis())
    assert c.channel_for("m1") != c.channel_for("m2")


def test_publish_message_carries_key_not_reply_body():
    """广播只带 key —— 不能把回复正文广播出去(那等于又开一个内容出口)。"""
    fake = _FakeRedis()
    c = RedisL2Cache(client=fake)
    c.set("m1", "退货运费谁承担", CacheEntry(reply="含敏感正文的回复"))
    fake.published.clear()
    c.invalidate("m1", "退货运费谁承担")
    assert fake.published, "失效应发广播"
    for _channel, message in fake.published:
        assert "含敏感正文的回复" not in message


# --- L2 读写与 TTL --------------------------------------------------------

def test_l2_set_carries_ttl():
    fake = _FakeRedis()
    c = RedisL2Cache(client=fake, ttl_seconds=1800)
    c.set("m1", "q", CacheEntry(reply="a"))
    assert fake.ttls, "L2 写入必须带 TTL(否则又是无界增长)"
    assert all(0 < v <= 1800 * 1.2 + 1 for v in fake.ttls.values())


def test_l2_roundtrip_returns_entry():
    c = RedisL2Cache(client=_FakeRedis())
    c.set("m1", "q", CacheEntry(reply="答案"))
    got = c.get("m1", "q")
    assert got is not None
    assert got.reply == "答案"


def test_l2_miss_returns_none():
    assert RedisL2Cache(client=_FakeRedis()).get("m1", "nope") is None


# --- 故障降级:不能带崩主链路 ---------------------------------------------

def test_l2_get_swallows_redis_failure():
    assert RedisL2Cache(client=_BrokenRedis()).get("m1", "q") is None


def test_l2_set_and_invalidate_swallow_redis_failure():
    c = RedisL2Cache(client=_BrokenRedis())
    c.set("m1", "q", CacheEntry(reply="a"))
    c.invalidate("m1", "q")  # 不抛即通过


# --- L1 ↔ L2 交互 --------------------------------------------------------

def test_l1_miss_falls_through_to_l2_and_backfills():
    fake = _FakeRedis()
    l2 = RedisL2Cache(client=fake)
    # created_at 必须给真实时刻:CacheEntry 默认 0.0,那样回填 L1 会被立刻判过期
    # (第一版自测就踩了这个,已在适配器里补 created_at 的序列化与还原)。
    l2.set("m1", "退货运费谁承担",
           CacheEntry(reply="来自L2的答案", created_at=time.time()))
    c = SemanticCache(l2=l2)
    hit = c.lookup("退货运费谁承担", merchant_id="m1")
    assert hit is not None and hit.reply == "来自L2的答案"
    # 回填后 L1 自己有了一份(size 增加)
    assert c.stats()["size"] >= 1


def test_store_writes_through_to_l2():
    fake = _FakeRedis()
    c = SemanticCache(l2=RedisL2Cache(client=fake))
    assert c.store("退货运费谁承担", _payload(), merchant_id="m1") is True
    assert fake.store, "store 应写入 L2"


def test_clear_publishes_invalidation():
    fake = _FakeRedis()
    c = SemanticCache(l2=RedisL2Cache(client=fake))
    c.store("退货运费谁承担", _payload(), merchant_id="m1")
    fake.published.clear()
    c.clear(merchant_id="m1")
    assert fake.published, "按租户清缓存应广播失效"


def test_l2_down_does_not_break_l1():
    """L2 挂了 L1 照常工作 —— 缓存是辅助路径,不得影响可用性。"""
    c = SemanticCache(l2=RedisL2Cache(client=_BrokenRedis()))
    assert c.store("退货运费谁承担", _payload(), merchant_id="m1") is True
    assert c.lookup("退货运费谁承担", merchant_id="m1") is not None


# --- 默认关闭:存量路径一字不变 -------------------------------------------

def test_no_redis_url_means_l2_disabled(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert build_l2() is None


def test_default_cache_has_no_l2():
    assert SemanticCache()._l2 is None


def test_redis_url_builds_l2(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    assert isinstance(build_l2(client_factory=lambda url: _FakeRedis()), RedisL2Cache)


def test_l2_factory_failure_disables_l2(monkeypatch):
    """连不上 Redis → L2 关闭,退回纯 L1(不阻断启动)。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")

    def _boom(url):
        raise ConnectionError("no redis")

    assert build_l2(client_factory=_boom) is None

def test_created_at_survives_l2_roundtrip():
    """TTL 必须锚在**原始入库时刻**:created_at 不过线会让回填条目立刻过期,
    或(若每次读都重置)让热门键永不过期。两种都错。"""
    c = RedisL2Cache(client=_FakeRedis())
    stamp = 1_700_000_000.0
    c.set("m1", "q", CacheEntry(reply="a", created_at=stamp))
    got = c.get("m1", "q")
    assert got is not None
    assert got.created_at == stamp
