"""R10(2026-11-21 企业工程评审):缓存 TTL 必须带抖动,避免同秒集中失效。

原判据命中「TTL 统一」反模式:`ttl_seconds=1800` 固定,所有 key 同一秒到期。
⚠️ 严重度按**实际形态**定 **P2** 而非照抄 reference 的 P1:
它是**进程内 L1、上限 512 条**,爆炸半径远小于 Redis 集群。

实现口径:**确定性抖动**(由 key 的 sha256 派生),不用 `random()`。
两个理由:
1. 项目质量判据明写「业务代码无裸 now()/random(),统一 Clock/IdGenerator 注入」;
2. **不能用内置 `hash()`** —— str 的 hash 带进程级随机盐(PYTHONHASHSEED),
   跨进程不可复现,会让"抖动"变成不可调试的随机行为。
sha256 派生既把到期时间打散(达到防雪崩目的),又可复现、可断言。
"""
from __future__ import annotations

from app.cache import SemanticCache


def _payload(reply: str = "退货期限是 7 天。") -> dict:
    return {"reply": reply, "citations": [], "intent": "policy_qa",
            "confidence": 0.9, "intent_reason": "命中退货时效"}


def test_effective_ttl_spreads_across_keys():
    """不同 key 的有效 TTL 必须**不同**(否则同秒集中失效,抖动无意义)。"""
    c = SemanticCache(ttl_seconds=1800)
    ttls = {c.effective_ttl_seconds(f"问题{i}") for i in range(40)}
    assert len(ttls) > 1, "所有 key 的有效 TTL 相同 = 抖动没生效"


def test_effective_ttl_stays_within_twenty_percent():
    c = SemanticCache(ttl_seconds=1800, ttl_jitter_ratio=0.2)
    for i in range(60):
        t = c.effective_ttl_seconds(f"问题{i}")
        assert 1800 * 0.8 <= t <= 1800 * 1.2, f"超出 ±20%: {t}"


def test_jitter_is_deterministic_across_instances():
    """同 key 在同参数的不同实例上必须得到同一 TTL(可复现、可调试)。"""
    a = SemanticCache(ttl_seconds=1800)
    b = SemanticCache(ttl_seconds=1800)
    for key in ("退货运费谁承担", "多久到账", "满多少包邮"):
        assert a.effective_ttl_seconds(key) == b.effective_ttl_seconds(key)


def test_zero_ratio_disables_jitter():
    """正向:ratio=0 时**精确等于 base**(保留原语义,便于对照与压测)。"""
    c = SemanticCache(ttl_seconds=1800, ttl_jitter_ratio=0.0)
    for i in range(20):
        assert c.effective_ttl_seconds(f"问题{i}") == 1800.0


def test_entry_expires_by_its_own_jittered_ttl():
    """行为级:条目按**自己的**有效 TTL 过期,不是统一 base。"""
    clock = {"now": 1000.0}
    c = SemanticCache(ttl_seconds=1000, ttl_jitter_ratio=0.2, clock=lambda: clock["now"])
    key = "退货运费谁承担"
    ttl = c.effective_ttl_seconds(key)
    assert c.store(key, _payload(), merchant_id="m1") is True
    clock["now"] = 1000.0 + ttl - 1
    assert c.lookup(key, merchant_id="m1") is not None, "未到自身 TTL 不应过期"
    clock["now"] = 1000.0 + ttl + 1
    assert c.lookup(key, merchant_id="m1") is None, "过了自身 TTL 必须过期"


def test_zero_ratio_keeps_exact_base_expiry():
    """正向对照:ratio=0 时过期点精确落在 base(原行为一字不变)。"""
    clock = {"now": 1000.0}
    c = SemanticCache(ttl_seconds=1800, ttl_jitter_ratio=0.0, clock=lambda: clock["now"])
    assert c.store("退货运费谁承担", _payload(), merchant_id="m1") is True
    clock["now"] = 1000.0 + 1799
    assert c.lookup("退货运费谁承担", merchant_id="m1") is not None
    clock["now"] = 1000.0 + 1801
    assert c.lookup("退货运费谁承担", merchant_id="m1") is None