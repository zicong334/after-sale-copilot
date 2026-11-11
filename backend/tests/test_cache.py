"""W10-2 语义缓存的单测。

覆盖口径:

- 同义改述命中(命中后返回缓存里的 reply + citations);
- 相似度低于阈值不命中;
- 写操作意图 / 含订单号 / 含 PII 占位符 三类请求不命中且不入库;
- TTL 过期后不命中(注入时钟,不用真等 30 分钟);
- ``clear()`` 后不命中;
- ``stats()`` 计数正确;
- 全程不发任何网络请求(把 socket 连通函数打成炸弹再跑一遍)。
"""
from __future__ import annotations

import socket

import pytest

from app.cache import CacheEntry, SemanticCache, _normalize, semantic_cache


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def cache() -> SemanticCache:
    """每个用例一个干净的缓存实例(不共享模块级单例)。"""
    return SemanticCache()


def _payload(reply: str = "退货期限是 7 天。") -> dict:
    """一份标准 payload(reply + citations + 意图信息)。"""
    return {
        "reply": reply,
        "citations": [{"chunk_id": "LP-01", "text": "签收后 7 天内可无理由退货。"}],
        "intent": "policy_qa",
        "confidence": 0.91,
        "intent_reason": "命中退货时效条款",
    }


# ---------------------------------------------------------------------------
# 命中 / 不命中
# ---------------------------------------------------------------------------

def test_paraphrase_hits_and_returns_cached_payload(cache: SemanticCache) -> None:
    """同义改述(简体/繁体 + 口语问法)命中,返回缓存里的 reply 与 citations。"""
    assert cache.store("退货期限是几天", _payload()) is True

    hit = cache.lookup("退貨期限要多久")

    assert hit is not None
    assert hit.reply == "退货期限是 7 天。"
    assert hit.citations == [
        {"chunk_id": "LP-01", "text": "签收后 7 天内可无理由退货。"}
    ]
    assert hit.intent == "policy_qa"
    assert hit.confidence == 0.91
    assert hit.intent_reason == "命中退货时效条款"
    assert cache.stats()["hits"] == 1


def test_exact_repeat_also_hits(cache: SemanticCache) -> None:
    """同一句原文重复提问直接命中。"""
    cache.store("运费什么时候到账", _payload("运费按地区计算。"))
    hit = cache.lookup("运费什么时候到账")
    assert hit is not None
    assert hit.reply == "运费按地区计算。"


def test_low_similarity_misses(cache: SemanticCache) -> None:
    """相似度低于阈值:相似度不够高就不命中,缓存原样保留。"""
    cache.store("退货期限是几天", _payload())

    hit = cache.lookup("运费什么时候到账")

    assert hit is None
    assert cache.stats()["size"] == 1
    assert cache.stats()["hits"] == 0
    assert cache.stats()["misses"] == 1


def test_lookup_returns_none_for_empty_text(cache: SemanticCache) -> None:
    """空文本既不可缓存也不命中(不崩)。"""
    cache.store("退货期限是几天", _payload())
    assert cache.lookup("") is None
    assert cache.lookup("   ") is None
    assert cache.stats()["size"] == 1


# ---------------------------------------------------------------------------
# 永不缓存:写操作意图
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "我要退货",
        "帮我取消订单",
        "转人工",
        "申请退款",
        "提交申请",
        "给我退钱",
        "马上退款",
    ],
)
def test_write_intent_is_never_cached(cache: SemanticCache, text: str) -> None:
    """写操作意图:不入库、不命中。"""
    assert cache.is_cacheable(text) is False
    assert cache.store(text, _payload()) is False
    assert cache.lookup(text) is None
    assert cache.stats()["size"] == 0
    assert cache.stats()["stores"] == 0


def test_policy_question_about_same_action_is_cacheable(cache: SemanticCache) -> None:
    """政策问句("退货期限是几天")是可缓存的 —— 判写意图不能只看动作词。"""
    assert cache.is_cacheable("退货期限是几天") is True
    assert cache.is_cacheable("怎么申请退货") is False  # 含强写意图词"申请退货"
    assert cache.is_cacheable("退款政策是什么") is True


def test_write_intent_intent_name_blocks_store(cache: SemanticCache) -> None:
    """意图名本身是写操作时也不缓存(intent=handoff)。"""
    assert cache.store("退货期限是几天", {**_payload(), "intent": "handoff"}) is False
    assert cache.stats()["size"] == 0


# ---------------------------------------------------------------------------
# 永不缓存:订单号 / PII
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "订单 seed-order-019 到哪了",
        "SO123456 发货了吗",
        "ORD-771 发货了吗",
        "订单号 88123456 什么时候到",
    ],
)
def test_order_number_is_never_cached(cache: SemanticCache, text: str) -> None:
    """含订单号的请求:不入库、不命中。"""
    assert cache.is_cacheable(text) is False
    assert cache.store(text, _payload()) is False
    assert cache.lookup(text) is None
    assert cache.stats()["size"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "我的电话是 [PHONE_1]",
        "我的地址是 [ADDR_1]",
        "我的证件号是 [ID_1]",
        "我的手机号 13800138000 怎么改",
    ],
)
def test_pii_is_never_cached(cache: SemanticCache, text: str) -> None:
    """含 PII 占位符或原文的请求:不入库、不命中。"""
    assert cache.is_cacheable(text) is False
    assert cache.store(text, _payload()) is False
    assert cache.lookup(text) is None
    assert cache.stats()["size"] == 0


# ---------------------------------------------------------------------------
# TTL / clear / stats
# ---------------------------------------------------------------------------

def test_ttl_expiry_misses(cache: SemanticCache) -> None:
    """TTL 过期后不命中(注入时钟,不做真等待)。"""
    clock = {"now": 1_000.0}
    cache = SemanticCache(ttl_seconds=1800, clock=lambda: clock["now"])

    assert cache.store("退货期限是几天", _payload()) is True
    clock["now"] = 1_000.0 + 1799.0
    assert cache.lookup("退货期限是几天") is not None

    clock["now"] = 1_000.0 + 1801.0
    assert cache.lookup("退货期限是几天") is None
    assert cache.stats()["size"] == 0


def test_default_ttl_is_thirty_minutes() -> None:
    """默认 TTL 是 30 分钟(1800 秒)。"""
    assert SemanticCache().ttl_seconds == 1800
    assert SemanticCache().threshold == 0.92


def test_clear_removes_entries(cache: SemanticCache) -> None:
    """clear() 之后不再命中。"""
    cache.store("退货期限是几天", _payload())
    assert cache.lookup("退货期限是几天") is not None

    cache.clear()

    assert cache.lookup("退货期限是几天") is None
    assert cache.stats()["size"] == 0


def test_stats_counters(cache: SemanticCache) -> None:
    """stats() 的 size/hits/misses/stores 计数正确。"""
    assert cache.stats() == {"size": 0, "hits": 0, "misses": 0, "stores": 0}

    cache.store("退货期限是几天", _payload())
    cache.store("运费什么时候到账", _payload("运费按地区计算。"))
    assert cache.stats()["size"] == 2
    assert cache.stats()["stores"] == 2

    cache.lookup("退货期限是几天")          # 命中
    cache.lookup("退貨期限要多久")           # 命中(同义改述)
    cache.lookup("发票什么时候开")           # 不命中
    assert cache.stats()["hits"] == 2
    assert cache.stats()["misses"] == 1
    assert cache.stats()["size"] == 2


def test_cache_entry_age_seconds(cache: SemanticCache) -> None:
    """CacheEntry.age_seconds() 给出条目存活秒数(不小于 0)。"""
    cache.store("退货期限是几天", _payload())
    entry = cache.lookup("退货期限是几天")
    assert isinstance(entry, CacheEntry)
    assert entry.age_seconds() >= 0.0


# ---------------------------------------------------------------------------
# 与 KB 热更新的联动 / 无网络
# ---------------------------------------------------------------------------

def test_hybrid_invalidate_cache_clears_semantic_cache() -> None:
    """KB 热更新(hybrid.invalidate_cache)顺带清空语义缓存。"""
    from app.retrieval import hybrid

    semantic_cache.clear()
    assert semantic_cache.store("退货期限是几天", _payload()) is True
    assert semantic_cache.stats()["size"] == 1

    hybrid.invalidate_cache()

    assert semantic_cache.stats()["size"] == 0
    assert semantic_cache.lookup("退货期限是几天") is None


def test_cache_never_opens_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """整个缓存路径不发任何网络请求(把 socket 连通函数打成炸弹)。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("语义缓存不允许发起网络请求")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    cache = SemanticCache()
    assert cache.store("退货期限是几天", _payload()) is True
    assert cache.lookup("退貨期限要多久") is not None
    assert cache.lookup("运费什么时候到账") is None
    assert cache.stats()["hits"] == 1


# ---------------------------------------------------------------------------
# W10 缺陷回归:ISS-46 / ISS-47 / ISS-55
# ---------------------------------------------------------------------------

def test_merchant_isolation_cross_tenant_miss(cache: SemanticCache) -> None:
    """ISS-46(P0):同一句问法在两个租户各存一份,互不命中、各回各的答案。"""
    assert cache.store("退货期限是几天", _payload("A 店:7 天。"), merchant_id="m_a")
    assert cache.store("退货期限是几天", _payload("B 店:15 天。"), merchant_id="m_b")

    hit_a = cache.lookup("退货期限是几天", merchant_id="m_a")
    hit_b = cache.lookup("退货期限是几天", merchant_id="m_b")
    assert hit_a is not None and hit_a.reply == "A 店:7 天。"
    assert hit_b is not None and hit_b.reply == "B 店:15 天。"
    # 第三个租户绝不能命中别人的自定义条款。
    assert cache.lookup("退货期限是几天", merchant_id="m_c") is None
    # 不传 merchant_id(全局口径)也看不到租户分区内的条目。
    assert cache.lookup("退货期限是几天") is None
    assert cache.stats()["size"] == 2


def test_clear_scoped_to_merchant(cache: SemanticCache) -> None:
    """ISS-46:按租户清缓存只清该租户,不误伤其他租户。"""
    cache.store("退货期限是几天", _payload("A 店:7 天。"), merchant_id="m_a")
    cache.store("退货期限是几天", _payload("B 店:15 天。"), merchant_id="m_b")

    cache.clear(merchant_id="m_a")

    assert cache.lookup("退货期限是几天", merchant_id="m_a") is None
    hit_b = cache.lookup("退货期限是几天", merchant_id="m_b")
    assert hit_b is not None and hit_b.reply == "B 店:15 天。"


def test_stopwords_do_not_reverse_negation(cache: SemanticCache) -> None:
    """ISS-47(P0):"没有收到"不能归一成"有收到"(否定→肯定)。"""
    # 归一化层:实义构词单字不再被子串抹除。
    assert _normalize("没有收到") == "没有收到"
    assert _normalize("没有收到") != _normalize("有收到")
    assert "会员" in _normalize("会员权益")
    assert "还是" not in _normalize("还是退货吧")  # 多字虚词仍整体删除

    # 缓存层:肯定说法绝不命中否定说法的缓存。
    assert cache.store("没有收到", _payload("请先查物流。")) is True
    assert cache.lookup("有收到") is None
    assert cache.lookup("没有收到") is not None


def test_lru_evicts_least_recently_used() -> None:
    """ISS-55(P2):max_entries 上限生效,按 LRU 淘汰最久未使用条目。"""
    cache = SemanticCache(threshold=1.0, max_entries=2)
    assert cache.max_entries == 2
    cache.store("问题一", _payload("答一"))
    cache.store("问题二", _payload("答二"))
    # 命中一次"问题一",把它刷成最近使用。
    assert cache.lookup("问题一") is not None
    cache.store("问题三", _payload("答三"))

    assert cache.stats()["size"] == 2
    assert cache.lookup("问题二") is None      # 最久未使用 → 先被淘汰
    assert cache.lookup("问题一") is not None  # 刚用过 → 保留
    assert cache.lookup("问题三") is not None


def test_default_max_entries_is_bounded() -> None:
    """ISS-55:默认实例有容量上限(512),避免无限增长。"""
    assert SemanticCache().max_entries == 512
    assert SemanticCache(max_entries=None).max_entries is None


def test_concurrent_lookup_store_stays_bounded() -> None:
    """ISS-55:并发读写不崩,容量始终不超过 max_entries。"""
    import threading

    cache = SemanticCache(threshold=1.0, max_entries=8)
    errors: list[BaseException] = []

    def _worker(prefix: str) -> None:
        try:
            for index in range(30):
                cache.store(f"{prefix}问题{index}", _payload(f"{prefix}答{index}"))
                cache.lookup(f"{prefix}问题{index}")
        except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(f"t{idx}",)) for idx in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert cache.stats()["size"] <= 8
