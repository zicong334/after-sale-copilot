"""R4/R5/R8 的**真 Redis 断言**(2026-11-21)。

⚠️ 这些断言**本机跑不了**(无 redis-server),所以在没有 ``REDIS_TEST_URL`` 时**整体跳过**。
它们存在的意义就是把评审里那三条「未运行时验证」变成**可执行的真验证**:

1. **集群总放行量 ≈ 阈值而非 N×** —— 两个独立限流器实例(模拟两个进程)共享同一 Redis,
   同一 key 的总放行量必须 ≈ burst,而不是 2×burst;
2. **EXPIRE 真的让键有界** —— 每个键都必须有正 TTL(进程内 `_buckets` 无界增长的反面);
3. **Lua 真原子性** —— 多线程并发扣同一 key,总放行量必须**恰好** burst,不能超发。

CI 的 pg-redis job 会设 ``REDIS_TEST_URL`` 指向 service container,届时这些断言真跑。
跑通后按 `docs/待验清单_20261121.md` §5 的规则清「未运行时验证」标记并贴 CI 原始输出。

**纪律**:本文件**不用假客户端**。假客户端只能验自写逻辑(那部分在
`test_ratelimit_redis.py` / `test_cache_l2.py`),证明不了上面三条。
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest

REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not REDIS_TEST_URL,
    reason="需真 Redis(设 REDIS_TEST_URL);本机无 redis-server,跳过而不是伪造通过",
)


@pytest.fixture()
def client():
    import redis

    c = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    c.ping()
    yield c
    # 只清本用例自己造的前缀,不 FLUSHDB(可能是共享实例)
    for key in c.scan_iter(match="rltest:*", count=500):
        c.delete(key)
    c.close()


def _prefix() -> str:
    return f"rltest:{uuid.uuid4().hex[:8]}"


def test_cluster_total_allowance_is_threshold_not_n_times(client):
    """两个实例共享 Redis:总放行量 ≈ burst,**不是 2×burst**。

    这是「本地计数器限流」反模式的正面反证:进程内实现下两个实例各放 burst 个。
    """
    from app.ratelimit_redis import RedisRateLimiter

    prefix = _prefix()
    burst = 10
    a = RedisRateLimiter(0.0, burst, client=client, prefix=prefix)  # rate=0 → 不补充
    b = RedisRateLimiter(0.0, burst, client=client, prefix=prefix)
    allowed = 0
    for i in range(burst * 3):
        lim = a if i % 2 == 0 else b
        ok, _ = lim.consume("same-subject", now=1000.0)
        allowed += int(ok)
    assert allowed == burst, f"集群总放行应恰好 {burst},实际 {allowed}(若为 {2*burst} 说明退化成各自计数)"


def test_keys_have_positive_ttl(client):
    """每个限流键都必须有正 TTL —— 这是 `_buckets` 无界增长的解法。"""
    from app.ratelimit_redis import RedisRateLimiter

    prefix = _prefix()
    lim = RedisRateLimiter(5, 10, client=client, prefix=prefix)
    for i in range(20):
        lim.consume(f"subject-{i}", now=1000.0)
    keys = list(client.scan_iter(match=f"{prefix}:*", count=500))
    assert len(keys) == 20
    for key in keys:
        ttl = client.ttl(key)
        assert ttl > 0, f"键 {key} 无 TTL(=-1 永不过期 / -2 不存在),无界增长未被解决"


def test_lua_consume_is_atomic_under_concurrency(client):
    """多线程并发扣同一 key:总放行量**恰好** burst,不能超发。

    单进程模拟证明不了原子性;这里用真 Redis + 真并发线程。
    """
    from app.ratelimit_redis import RedisRateLimiter

    prefix = _prefix()
    burst = 25
    lim = RedisRateLimiter(0.0, burst, client=client, prefix=prefix)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        local = []
        for _ in range(20):
            ok, _retry = lim.consume("hot-subject", now=2000.0)
            local.append(ok)
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 160
    assert sum(results) == burst, f"并发下应恰好放行 {burst},实际 {sum(results)}(超发=Lua 非原子)"


def test_l2_roundtrip_and_invalidate_on_real_redis(client):
    """L2 在真 Redis 上的读写与失效(跨实例语义的基础)。"""
    from app.cache import CacheEntry
    from app.cache_l2 import RedisL2Cache

    l2 = RedisL2Cache(client=client)
    merchant = f"m-{uuid.uuid4().hex[:6]}"
    l2.set(merchant, "退货运费谁承担", CacheEntry(reply="真 Redis 答案", created_at=1_700_000_000.0))
    got = l2.get(merchant, "退货运费谁承担")
    assert got is not None and got.reply == "真 Redis 答案"
    assert got.created_at == 1_700_000_000.0, "created_at 必须过线(TTL 锚原始入库时刻)"
    # 另一个实例(同 Redis)必须读到同一条 —— 跨实例复用的核心
    other = RedisL2Cache(client=client)
    assert other.get(merchant, "退货运费谁承担") is not None
    l2.invalidate(merchant, "退货运费谁承担")
    assert other.get(merchant, "退货运费谁承担") is None, "失效后其他实例不应再命中"