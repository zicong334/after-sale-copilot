"""模型网关(W10-1):主备热备 + 熔断器。

设计要点
--------
1. **三档路由**:`primary`(DeepSeek 真实客户端)→ `fallback`(Minimax 真实客户端)
   → `degraded`(规则引擎 `FakeLLM`,MOCK)。任一真实档位出现**熔断失败**
   (5xx / 429 / 超时),立即由下一档接管**同一请求**;两档真实客户端都不可用时
   落到规则引擎,保证链路永远有输出。
2. **熔断判定**:只有 5xx / 429 / 超时(以及无状态码的网络类错误)计为熔断失败;
   **401 / 402 / 403(鉴权、余额)与 4xx 配置错误一律不计失败、直接上抛**——
   这类错误重试和降级都救不了,悄悄降级只会把故障藏起来。
3. **熔断状态机**(每档一个):`closed` →(连续 N=3 次熔断失败)→ `open`
   →(停留 30s)→ `half-open`(只放 1 个探针)→ 探针成功 → `closed`;探针失败
   → 回到 `open`。每次状态转移都记进 `CircuitBreaker.transitions`。
4. **trace 事件**:每次状态切换/降级写一条 `router_failover` 事件;写盘失败
   (无 session / DB 异常)不影响主流程。
5. **零真实调用**:本模块不发任何网络请求,只按 `LLMClient` 契约调度三档客户端。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import httpx

from app.llm.base import FakeLLM, LLMClient, Message
from app.llm.real_client import LLMApiError, UsageStats
from app.trace import record_event

#: 路由降级事件名(写入 tool_events,命名空间区分事件类别)。
EVENT_ROUTER_FAILOVER = "router_failover"

#: 档位名(与 trace payload 的 from/to 取值一致)。
TIER_PRIMARY = "primary"
TIER_FALLBACK = "fallback"
TIER_DEGRADED = "degraded"

#: 档位被熔断跳过时的 error_code。
_OPEN_CODE = {TIER_PRIMARY: "primary_open", TIER_FALLBACK: "fallback_open"}
#: 档位调用失败(熔断失败)时的 error_code:备胎失败即落到规则档。
_FAIL_CODE = {TIER_PRIMARY: "primary_failed", TIER_FALLBACK: "degraded"}
#: 熔断器状态转移时的 error_code。
_STATE_CODE = {"open": "breaker_open", "half-open": "breaker_half_open", "closed": "breaker_closed"}


def is_circuit_failure(exc: BaseException) -> bool:
    """判定异常是否计入熔断失败。

    `LLMApiError` 且 `status_code >= 500` 或 `== 429` → 计入;`LLMApiError`
    且无状态码(real_client 把超时/网络错误包成这种)→ 计入;
    `httpx.TimeoutException` → 计入;401/402/403/其它 4xx 配置错误 → **不计入**。
    """
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, LLMApiError):
        status_code = exc.status_code
        if status_code is None:
            return True
        return status_code == 429 or status_code >= 500
    return False


class CircuitBreaker:
    """单个档位的熔断器:closed / open / half-open 三态。

    线程安全性说明:本类按"单次请求串行"使用(编排层同一时刻只跑一路),
    未加锁;若将来并发调用,需要自行加锁。
    """

    CLOSED = "closed"  # 闭合(正常放行)
    OPEN = "open"  # 打开(拒绝放行,等待恢复超时)
    HALF_OPEN = "half-open"  # 半开(放行探针)

    def __init__(
        self, *, name: str = "client", failure_threshold: int = 3,
        recovery_timeout: float = 30.0, half_open_limit: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = float(recovery_timeout)
        self.half_open_limit = max(1, int(half_open_limit))
        self._clock = clock
        self._state = self.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probes_in_flight = 0
        #: 状态转移流水:每次转移 append 一条 `{"from","to","at","reason"}`。
        self.transitions: list[dict[str, str]] = []

    @property
    def state(self) -> str:
        """当前状态(closed / open / half-open)。"""
        return self._state

    @property
    def failure_count(self) -> int:
        """连续熔断失败次数(成功即清零)。"""
        return self._failure_count

    def allow_request(self) -> bool:
        """是否放行本次请求。

        closed 直接放行;open 未到恢复超时则拒绝;open 已到恢复超时则转 half-open
        并放行 1 个探针;half-open 只放行 `half_open_limit` 个探针,其余拒绝。
        """
        if self._state == self.CLOSED:
            return True
        if self._state == self.OPEN:
            if self._seconds_since_open() < self.recovery_timeout:
                return False
            self._transition(self.HALF_OPEN, reason=f"熔断停留满 {self.recovery_timeout}s,转半开探测")
        if self._probes_in_flight >= self.half_open_limit:
            return False
        self._probes_in_flight += 1
        return True

    def record_success(self) -> None:
        """记一次成功:清零失败计数;半开状态下探针成功即回到 closed。"""
        self._probes_in_flight = max(0, self._probes_in_flight - 1)
        self._failure_count = 0
        if self._state == self.HALF_OPEN:
            self._transition(self.CLOSED, reason="半开探针成功,恢复放行")

    def record_failure(self, *, reason: str = "") -> None:
        """记一次熔断失败:连续达阈值则开路;半开探针失败则重新开路。"""
        self._probes_in_flight = max(0, self._probes_in_flight - 1)
        if self._state == self.HALF_OPEN:
            self._failure_count = self.failure_threshold
            self._transition(self.OPEN, reason=reason or "半开探针失败,重新熔断")
            self._opened_at = self._clock()
            return
        if self._state == self.OPEN:
            return
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._transition(self.OPEN, reason=reason or f"连续 {self._failure_count} 次熔断失败")
            self._opened_at = self._clock()

    def _transition(self, to_state: str, *, reason: str) -> None:
        """记录一次状态转移(并写入转移流水)。"""
        from_state = self._state
        self._state = to_state
        self.transitions.append(
            {
                "from": from_state,
                "to": to_state,
                "at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            }
        )

    def _seconds_since_open(self) -> float:
        """距离开路过去了多少秒(没开路则返回 0)。"""
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._clock() - self._opened_at)


class ModelRouter(LLMClient):
    """模型网关:主用 → 热备 → 规则引擎,带熔断与降级事件。"""

    def __init__(
        self,
        primary: LLMClient,
        fallback: LLMClient,
        degraded: LLMClient | None = None,
        *,
        session: Any | None = None,
        trace_id: str | None = None,
        breaker: CircuitBreaker | None = None,
        fallback_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.degraded = degraded if degraded is not None else FakeLLM()
        self.primary_breaker = breaker or CircuitBreaker(name=TIER_PRIMARY)
        self.fallback_breaker = fallback_breaker or CircuitBreaker(name=TIER_FALLBACK)
        self._session = session
        self._trace_id = trace_id

    @property
    def model_name(self) -> str:
        """模型标识:看得出主/备各挂的是谁。"""
        return f"router[primary={self.primary.model_name}, fallback={self.fallback.model_name}]"

    @property
    def active_tier(self) -> str:
        """当前生效档位(只读判定,不消费探针名额)。"""
        if self.primary_breaker.state == CircuitBreaker.CLOSED:
            return TIER_PRIMARY
        if self.fallback_breaker.state == CircuitBreaker.CLOSED:
            return TIER_FALLBACK
        return TIER_DEGRADED

    @property
    def usage(self) -> UsageStats:
        """三档客户端的用量聚合(取不到就跳过,不抛异常)。"""
        total = UsageStats()
        for client in (self.primary, self.fallback, self.degraded):
            stats = getattr(client, "usage", None)
            if stats is None:
                continue
            try:
                total = total + stats
            except Exception:  # pragma: no cover - 第三方 usage 实现异常不应影响主流程
                continue
        return total

    @property
    def transitions(self) -> list[dict[str, str]]:
        """两档熔断器的状态转移流水合并视图(每条带 `tier` 标注)。"""
        merged: list[dict[str, str]] = []
        for tier, breaker in (
            (TIER_PRIMARY, self.primary_breaker),
            (TIER_FALLBACK, self.fallback_breaker),
        ):
            for item in breaker.transitions:
                entry = dict(item)
                entry["tier"] = tier
                merged.append(entry)
        return merged

    def complete(self, messages: list[Message], schema: dict[str, Any] | None = None) -> str:
        """按 主 → 备 → 规则 的顺序取结果;档位失败立即由下一档接管。"""
        for tier, client, breaker, next_tier in self._chain():
            if not self._enter_tier(tier, breaker, next_tier):
                continue
            try:
                text = client.complete(messages, schema)
            except Exception as exc:
                if not is_circuit_failure(exc):
                    raise
                self._fail_tier(tier, breaker, next_tier, exc)
                continue
            self._succeed_tier(tier, breaker, next_tier)
            return text
        return self._use_degraded(messages, schema)

    def stream_text(self, messages: list[Message], schema: dict[str, Any] | None = None) -> Iterator[str]:
        """流式取正文:与 complete 同档位顺序。

        **还没吐出任何增量**时允许换下一档从头重来(不重复已吐内容);
        已经吐出增量则不再切换,直接抛错(否则会重复内容)。
        """
        for tier, client, breaker, next_tier in self._chain():
            if not self._enter_tier(tier, breaker, next_tier):
                continue
            emitted = False
            try:
                for piece in client.stream_text(messages, schema):
                    emitted = True
                    yield piece
            except Exception as exc:
                if emitted or not is_circuit_failure(exc):
                    raise
                self._fail_tier(tier, breaker, next_tier, exc)
                continue
            self._succeed_tier(tier, breaker, next_tier)
            return
        for piece in self.degraded.stream_text(messages, schema):
            yield piece

    def attach_trace(self, session: Any | None, trace_id: str | None) -> "ModelRouter":
        """绑定写 trace 事件用的 session / trace_id(链式返回自身)。"""
        self._session = session
        self._trace_id = trace_id
        return self

    def healthcheck(self) -> dict[str, Any]:
        """只探一次备胎连通性(委派 fallback;不支持则返回失败原因)。"""
        probe = getattr(self.fallback, "healthcheck", None)
        if not callable(probe):
            return self._probe_failure("备胎客户端不支持 healthcheck")
        try:
            result = probe()
        except Exception as exc:
            return self._probe_failure(f"{type(exc).__name__}: {exc}")
        if not isinstance(result, dict):
            return self._probe_failure("healthcheck 返回格式非法")
        return result

    def close(self) -> None:
        """关闭三档客户端(若它们支持 close)。"""
        for client in (self.primary, self.fallback, self.degraded):
            closer = getattr(client, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - 关闭失败不影响结果
                    pass

    def _probe_failure(self, error: str) -> dict[str, Any]:
        """探活失败的统一回执(原文 dict)。"""
        return {
            "ok": False,
            "provider": getattr(self.fallback, "provider", None),
            "model": self.fallback.model_name,
            "error": error,
        }

    def _chain(self) -> tuple[tuple[str, LLMClient, CircuitBreaker, str], ...]:
        """档位链:(档位名, 客户端, 熔断器, 下一档位名)。"""
        return (
            (TIER_PRIMARY, self.primary, self.primary_breaker, TIER_FALLBACK),
            (TIER_FALLBACK, self.fallback, self.fallback_breaker, TIER_DEGRADED),
        )

    def _enter_tier(self, tier: str, breaker: CircuitBreaker, next_tier: str) -> bool:
        """尝试进入某档:放行返回 True,被熔断拒绝返回 False(并写降级事件)。"""
        before = len(breaker.transitions)
        allowed = breaker.allow_request()
        self._flush_transitions(tier, breaker, next_tier, before)
        if allowed:
            return True
        self._emit_failover(
            from_tier=tier,
            to_tier=next_tier,
            reason=f"{tier} 熔断开启,直接降级到 {next_tier}",
            breaker_state=breaker.state,
            error="",
            error_code=_OPEN_CODE.get(tier, "breaker_open"),
        )
        return False

    def _fail_tier(
        self, tier: str, breaker: CircuitBreaker, next_tier: str, exc: BaseException
    ) -> None:
        """档位调用失败(熔断失败):记失败并写降级事件。"""
        before = len(breaker.transitions)
        breaker.record_failure(reason=f"{tier} 调用失败:{exc}")
        reason = f"{tier} 调用失败,降级到 {next_tier}"
        if len(breaker.transitions) > before:
            reason = f"{tier} 调用失败,降级到 {next_tier}({breaker.transitions[-1]['reason']})"
        self._emit_failover(
            from_tier=tier, to_tier=next_tier, reason=reason,
            breaker_state=breaker.state, error=str(exc),
            error_code=_FAIL_CODE.get(tier, "degraded"),
        )

    def _succeed_tier(self, tier: str, breaker: CircuitBreaker, next_tier: str) -> None:
        """档位调用成功:记成功;若触发了状态转移(如半开→闭合)则写事件。"""
        before = len(breaker.transitions)
        breaker.record_success()
        self._flush_transitions(tier, breaker, next_tier, before)

    def _flush_transitions(
        self, tier: str, breaker: CircuitBreaker, next_tier: str, before: int
    ) -> None:
        """把新产生的状态转移写成 `router_failover` 事件。"""
        for item in breaker.transitions[before:]:
            self._emit_failover(
                from_tier=tier, to_tier=next_tier, reason=item["reason"],
                breaker_state=item["to"], error="",
                error_code=_STATE_CODE.get(item["to"], "breaker_state"),
            )

    def _use_degraded(self, messages: list[Message], schema: dict[str, Any] | None) -> str:
        """落到规则引擎档(MOCK)并返回其结果(降级事件已由上一档的失败/熔断覆盖)。"""
        return self.degraded.complete(messages, schema)

    def _emit_failover(
        self, *, from_tier: str, to_tier: str, reason: str, breaker_state: str,
        error: str, error_code: str,
    ) -> None:
        """写一条 `router_failover` trace 事件(失败不影响主流程)。"""
        session = self._session
        trace_id = self._trace_id
        if session is None or not trace_id:
            return
        payload = {
            "from": from_tier,
            "to": to_tier,
            "reason": reason,
            "breaker_state": breaker_state,
            "error": error,
        }
        try:
            record_event(
                session,
                trace_id=trace_id,
                event=EVENT_ROUTER_FAILOVER,
                payload=payload,
                ok=False,
                error_code=error_code,
            )
        except Exception:  # pragma: no cover - 写盘失败不得影响主流程
            return


__all__ = [
    "CircuitBreaker", "EVENT_ROUTER_FAILOVER", "ModelRouter",
    "TIER_DEGRADED", "TIER_FALLBACK", "TIER_PRIMARY", "is_circuit_failure",
]
