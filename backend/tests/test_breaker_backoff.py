"""R12(2026-11-21 企业工程评审核验):熔断冷却必须递增,不能永远固定 30s。

核验读全 `llm/router.py` 后,R12 的实质缺陷**收敛为一条**:
`recovery_timeout` 固定,半开探针失败只把 `_opened_at` 重置为当前时刻,
**下次仍是同一个 30s** —— 下游长期不可用时会**每 30s 撞一次满超时,永不退避**。
reference 判据:半开失败 → 重新 open 且**冷却翻倍(上限 5min)**。

同轮核验还确认了两条**正面事实**(不在本文件重复测,已有存量覆盖):
- `is_circuit_failure()` 只把 5xx/429/超时/网络计入,**401/402/403 配置类 4xx 不计入**;
- 流式**已吐出字节就不做档位切换**(不能把已发出的字节收回)。

本文件只测冷却递增与探测数,不动上述两条既有正确行为。
"""
from __future__ import annotations

from app.llm.router import CircuitBreaker


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _open(breaker: CircuitBreaker) -> None:
    breaker.record_failure(reason="上游 500")


def test_first_open_uses_base_cooldown():
    """正向:**首次**开路仍用 base,不能一上来就退避(防守卫过度)。"""
    clock = _Clock()
    b = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, clock=clock)
    _open(b)
    clock.advance(9.0)
    assert b.allow_request() is False
    clock.advance(1.0)
    assert b.allow_request() is True
    assert b.state == CircuitBreaker.HALF_OPEN


def test_probe_failure_doubles_cooldown():
    """半开探针失败 → 冷却翻倍:原 base 10s,再等 10s 仍应拒绝,等满 20s 才探测。"""
    clock = _Clock()
    b = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, clock=clock)
    _open(b)
    clock.advance(10.0)
    assert b.allow_request() is True          # 第一次探测
    b.record_failure(reason="上游仍 500")
    assert b.state == CircuitBreaker.OPEN
    clock.advance(10.0)                        # 只等了原 base
    assert b.allow_request() is False, "冷却应已翻倍到 20s,不能仍按 10s 放行"
    clock.advance(10.0)                        # 累计 20s
    assert b.allow_request() is True


def test_cooldown_escalation_is_capped():
    """翻倍必须有上限(reference:5min),不能无限退避到下游恢复后还长时间不探测。"""
    clock = _Clock()
    b = CircuitBreaker(
        failure_threshold=1, recovery_timeout=10.0,
        max_recovery_timeout=40.0, clock=clock,
    )
    _open(b)
    for _ in range(6):                         # 反复探测失败,冷却应停在 40s
        clock.advance(1000.0)
        assert b.allow_request() is True
        b.record_failure(reason="持续 500")
    clock.advance(39.0)
    assert b.allow_request() is False
    clock.advance(1.0)
    assert b.allow_request() is True, "冷却应被上限 40s 截断,而不是 10×2^n"


def test_successful_recovery_resets_cooldown():
    """恢复后冷却回到 base —— 否则一次故障会让后续永久处于长退避。"""
    clock = _Clock()
    b = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, clock=clock)
    _open(b)
    clock.advance(10.0)
    b.allow_request()
    b.record_failure(reason="探针失败")        # 冷却翻倍到 20s
    clock.advance(20.0)
    b.allow_request()
    b.record_success()                         # 恢复
    assert b.state == CircuitBreaker.CLOSED
    _open(b)                                   # 再故障
    clock.advance(9.0)
    assert b.allow_request() is False
    clock.advance(1.0)
    assert b.allow_request() is True, "恢复后应回到 base 10s,而不是沿用翻倍值"


def test_default_half_open_limit_allows_more_than_one_probe():
    """探测数默认应 >1(reference 建议 5-10),单探针样本过小。"""
    b = CircuitBreaker()
    assert b.half_open_limit > 1