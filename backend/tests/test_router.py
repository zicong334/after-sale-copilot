"""W10-1 模型网关(主备热备 + 熔断器)的**密封**单元测试。

约束
----
- **零真实 API 调用**:所有客户端都是脚本化假客户端;``httpx.Client`` 的发送方法
  被 autouse fixture 全部封死(一旦有人真发请求,测试直接失败)。
- 熔断器用**注入的假时钟**驱动状态转移,测试不 sleep、不等真实 30s。
- 事件断言走真实的 ``app.trace.record_event`` 写盘路径,只把 session 换成
  记录调用的假 session(能读到 payload / ok / error_code)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

import httpx
import pytest

from app.llm import build_llm
from app.llm.base import LLMClient
from app.llm.minimax_client import MinimaxRealClient
from app.llm.real_client import LLMApiError, LLMConfigError, UsageStats
from app.llm.router import CircuitBreaker, ModelRouter

#: 明显的占位串(不是真 key),用于断言"异常/回执里不出现 key"。
_PLACEHOLDER_KEY = "unit-test-placeholder-not-a-real-key"

_MESSAGES = [{"role": "user", "content": "这个订单还能退吗"}]


# ---------------------------------------------------------------------------
# 假客户端 / 假 session / 假时钟
# ---------------------------------------------------------------------------


@dataclass
class _StreamThenFail:
    """先吐若干增量、再抛异常(模拟"吐了一半挂了")。"""

    pieces: list[str]
    error: BaseException


class _ScriptedClient(LLMClient):
    """按脚本返回结果/抛异常的假客户端(零网络)。"""

    def __init__(
        self,
        name: str,
        script: list[Any] | None = None,
        *,
        model: str | None = None,
        usage: UsageStats | None = None,
    ) -> None:
        self.name = name
        self.script = list(script or [])
        self.calls: list[str] = []
        self._model = model or f"{name}-model"
        self._usage = usage

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def usage(self) -> UsageStats | None:
        return self._usage

    def _next(self, kind: str) -> Any:
        self.calls.append(kind)
        if not self.script:
            raise AssertionError(f"{self.name} 的脚本已耗尽:第 {len(self.calls)} 次调用")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def complete(self, messages, schema=None) -> str:
        return str(self._next("complete"))

    def stream_text(self, messages, schema=None) -> Iterator[str]:
        item = self._next("stream")
        if isinstance(item, _StreamThenFail):
            for piece in item.pieces:
                yield piece
            raise item.error
        for piece in item:
            yield piece


class _HealthClient(_ScriptedClient):
    """带 healthcheck 的假客户端(统计被调用次数)。"""

    def __init__(self, *args: Any, result: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.result = result
        self.health_calls = 0

    def healthcheck(self) -> Any:
        self.health_calls += 1
        return self.result


class _FakeSession:
    """记录 trace 事件的假 session(配合真实的 record_event 写盘路径)。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def scalar(self, statement: Any) -> None:
        """next_step 查 max(step):假 session 恒定返回空(步号从 0 开始)。"""
        return None

    def add(self, obj: Any) -> None:
        self.events.append(obj)

    def flush(self) -> None:
        return None


class _FakeClock:
    """可推进的假时钟(驱动熔断恢复超时)。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _api_error(message: str, status_code: int | None) -> LLMApiError:
    """构造一个带状态码的 API 错误。"""
    return LLMApiError(message, status_code=status_code)


def _payloads(session: _FakeSession) -> list[dict[str, Any]]:
    """取出假 session 里所有事件的 payload(已解析 JSON)。"""
    return [json.loads(event.payload) for event in session.events]


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """封死 httpx 的发送方法:测试里一旦出现真实请求就直接失败。"""

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError("测试中不允许真实网络调用")

    for name in ("post", "get", "put", "delete", "request", "stream"):
        monkeypatch.setattr(httpx.Client, name, _boom)


# ---------------------------------------------------------------------------
# 熔断器状态机
# ---------------------------------------------------------------------------


def test_circuit_breaker_state_machine_full_path():
    """状态机转移表:closed → open → half-open → closed。"""
    clock = _FakeClock()
    breaker = CircuitBreaker(
        name="primary", failure_threshold=3, recovery_timeout=30.0, clock=clock
    )

    assert breaker.state == "closed"
    assert breaker.allow_request() is True

    breaker.record_failure(reason="上游 502")
    breaker.record_failure(reason="上游 502")
    assert breaker.state == "closed"  # 未达阈值,不开路
    breaker.record_failure(reason="上游 502")
    assert breaker.state == "open"
    assert breaker.allow_request() is False  # open 期间拒绝

    clock.advance(30.0)
    assert breaker.allow_request() is True  # 超时后放 1 个探针
    assert breaker.state == "half-open"
    assert breaker.allow_request() is False  # 探针未结束,不再放

    breaker.record_success()
    assert breaker.state == "closed"
    assert [(t["from"], t["to"]) for t in breaker.transitions] == [
        ("closed", "open"),
        ("open", "half-open"),
        ("half-open", "closed"),
    ]
    assert all(item["at"] and item["reason"] for item in breaker.transitions)


def test_circuit_breaker_half_open_probe_failure_reopens():
    """半开探针失败 → 回到 open,并重新开始计时。"""
    clock = _FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, clock=clock)
    breaker.record_failure(reason="上游 500")
    assert breaker.state == "open"

    clock.advance(10.0)
    assert breaker.allow_request() is True
    assert breaker.state == "half-open"

    breaker.record_failure(reason="上游 500")
    assert breaker.state == "open"
    assert breaker.allow_request() is False


def test_circuit_breaker_success_resets_failure_count():
    """成功即清零连续失败计数(不算"连续")。"""
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failure_count == 0
    breaker.record_failure()
    assert breaker.state == "closed"


# ---------------------------------------------------------------------------
# 路由:主 → 备 → 规则
# ---------------------------------------------------------------------------


def test_primary_failures_open_breaker_and_fallback_takes_over():
    """主连败 3 次 → 熔断 open → 之后请求由备接管;open 期间主不再被调用。"""
    primary = _ScriptedClient(
        "primary", [_api_error("上游 502", 502)] * 3 + ["主用恢复"]
    )
    fallback = _ScriptedClient("fallback", ["备胎答复"] * 5)
    router = ModelRouter(primary, fallback)

    for _ in range(3):
        assert router.complete(_MESSAGES) == "备胎答复"

    assert router.primary_breaker.state == "open"
    assert len(primary.calls) == 3

    # open 期间:主不再被调用,请求继续由备接管
    assert router.complete(_MESSAGES) == "备胎答复"
    assert len(primary.calls) == 3
    assert len(fallback.calls) == 4


def test_half_open_probe_success_returns_to_primary():
    """half-open 放 1 个探针 → 探针成功 → 回 closed → 之后又走主。"""
    clock = _FakeClock()
    breaker = CircuitBreaker(name="primary", recovery_timeout=30.0, clock=clock)
    primary = _ScriptedClient(
        "primary",
        [_api_error("上游 502", 502)] * 3 + ["主用答复", "主用答复二"],
    )
    fallback = _ScriptedClient("fallback", ["备胎答复"] * 5)
    router = ModelRouter(primary, fallback, breaker=breaker)

    for _ in range(3):
        router.complete(_MESSAGES)
    assert router.primary_breaker.state == "open"

    clock.advance(31.0)
    assert router.complete(_MESSAGES) == "主用答复"  # 探针命中主用
    assert router.primary_breaker.state == "closed"
    assert len(primary.calls) == 4

    assert router.complete(_MESSAGES) == "主用答复二"  # 恢复后继续走主
    assert len(primary.calls) == 5


def test_fallback_failure_falls_back_to_rule_tier():
    """备也失败 → 落规则 tier(degraded 被调用并返回其结果)。"""
    primary = _ScriptedClient("primary", [_api_error("上游 502", 502)] * 4)
    fallback = _ScriptedClient("fallback", [_api_error("备胎 503", 503)] * 4)
    degraded = _ScriptedClient("degraded", ["规则答复"] * 4)
    router = ModelRouter(primary, fallback, degraded)

    for _ in range(3):
        assert router.complete(_MESSAGES) == "规则答复"
    assert router.fallback_breaker.state == "open"

    # 两档真实客户端都熔断 → 直接落规则 tier,主/备都不再被调用
    assert router.complete(_MESSAGES) == "规则答复"
    assert len(primary.calls) == 3
    assert len(fallback.calls) == 3
    assert len(degraded.calls) == 4


def test_failover_events_are_recorded_with_payload():
    """每次切换都写了 router_failover 事件(payload / ok / error_code 可断言)。"""
    session = _FakeSession()
    primary = _ScriptedClient("primary", [_api_error("上游 502", 502)] * 3)
    fallback = _ScriptedClient("fallback", ["备胎答复"] * 3)
    router = ModelRouter(primary, fallback, session=session, trace_id="trace-1")

    for _ in range(3):
        router.complete(_MESSAGES)

    payloads = _payloads(session)
    assert len(payloads) == 3
    assert all(item["event"] == "router_failover" for item in payloads)
    assert [item["from"] for item in payloads] == ["primary"] * 3
    assert [item["to"] for item in payloads] == ["fallback"] * 3
    assert [item["breaker_state"] for item in payloads] == ["closed", "closed", "open"]
    assert all("502" in item["error"] for item in payloads)
    assert all(item["reason"] for item in payloads)

    assert [event.ok for event in session.events] == [False] * 3
    assert [event.error_code for event in session.events] == ["primary_failed"] * 3
    assert {event.trace_id for event in session.events} == {"trace-1"}
    assert {event.tool_name for event in session.events} == {"router_failover"}

    # 熔断状态转移也有流水
    assert router.transitions[-1]["tier"] == "primary"
    assert router.transitions[-1]["to"] == "open"
    assert router.transitions[-1]["from"] == "closed"


def test_open_breaker_writes_primary_open_event():
    """主熔断开启后,降级事件带 primary_open 错误码。"""
    session = _FakeSession()
    primary = _ScriptedClient("primary", [_api_error("上游 502", 502)] * 4)
    fallback = _ScriptedClient("fallback", ["备胎答复"] * 4)
    router = ModelRouter(primary, fallback, session=session, trace_id="trace-2")

    for _ in range(4):
        router.complete(_MESSAGES)

    codes = [event.error_code for event in session.events]
    assert "primary_open" in codes
    payloads = _payloads(session)
    open_payload = next(
        item for item in payloads if item["breaker_state"] == "open" and item["from"] == "primary"
    )
    assert open_payload["to"] == "fallback"


def test_auth_error_is_not_counted_as_circuit_failure():
    """401/403 不计熔断失败:状态仍 closed,也不降级到备胎。"""
    session = _FakeSession()
    primary = _ScriptedClient("primary", [_api_error("鉴权失败 401", 401)])
    fallback = _ScriptedClient("fallback", ["备胎答复"])
    router = ModelRouter(primary, fallback, session=session, trace_id="trace-3")

    with pytest.raises(LLMApiError):
        router.complete(_MESSAGES)

    assert router.primary_breaker.state == "closed"
    assert router.primary_breaker.failure_count == 0
    assert fallback.calls == []  # 鉴权错不降级(降级只会掩盖故障)
    assert session.events == []


def test_config_error_is_not_counted_as_circuit_failure():
    """4xx 配置错误(LLMConfigError)直接上抛,不计熔断失败。"""
    primary = _ScriptedClient("primary", [LLMConfigError("请求被拒 400")])
    fallback = _ScriptedClient("fallback", ["备胎答复"])
    router = ModelRouter(primary, fallback)

    with pytest.raises(LLMConfigError):
        router.complete(_MESSAGES)
    assert router.primary_breaker.state == "closed"
    assert fallback.calls == []


def test_model_name_usage_and_active_tier():
    """model_name 看得出主备档位;usage 聚合并跳过取不到的档位。"""
    primary = _ScriptedClient(
        "primary",
        ["主用答复"],
        model="deepseek-flash",
        usage=UsageStats(prompt_tokens=10, completion_tokens=5, total_tokens=15, requests=1),
    )
    fallback = _ScriptedClient(
        "fallback",
        ["备胎答复"],
        model="MiniMax-M3.1-Flash",
        usage=UsageStats(prompt_tokens=3, completion_tokens=2, total_tokens=5, requests=1),
    )
    degraded = _ScriptedClient("degraded", ["规则答复"])  # 无 usage
    router = ModelRouter(primary, fallback, degraded)

    assert router.model_name == "router[primary=deepseek-flash, fallback=MiniMax-M3.1-Flash]"
    assert router.active_tier == "primary"

    usage = router.usage
    assert usage.prompt_tokens == 13
    assert usage.total_tokens == 20
    assert usage.requests == 2


def test_router_healthcheck_delegates_to_fallback():
    """healthcheck 只委派备胎打一次。"""
    fallback = _HealthClient("fallback", result={"ok": True, "provider": "minimax"})
    router = ModelRouter(_ScriptedClient("primary"), fallback)
    result = router.healthcheck()
    assert result["ok"] is True
    assert fallback.health_calls == 1


def test_router_healthcheck_without_support_reports_error():
    """备胎不支持 healthcheck 时返回失败原因,不抛异常。"""
    router = ModelRouter(_ScriptedClient("primary"), _ScriptedClient("fallback"))
    result = router.healthcheck()
    assert result["ok"] is False
    assert result["error"]


def test_attach_trace_binds_session_and_trace_id():
    """attach_trace 绑定 session/trace_id 后才开始写事件。"""
    session = _FakeSession()
    primary = _ScriptedClient("primary", [_api_error("上游 502", 502)] * 2)
    fallback = _ScriptedClient("fallback", ["备胎答复"] * 2)
    router = ModelRouter(primary, fallback)

    router.complete(_MESSAGES)
    assert session.events == []

    router.attach_trace(session, "trace-4")
    router.complete(_MESSAGES)
    assert len(session.events) == 1
    assert session.events[0].trace_id == "trace-4"


def test_trace_write_failure_does_not_break_request():
    """写 trace 失败(假 session 抛异常)不影响主流程返回。"""

    class _BrokenSession(_FakeSession):
        def add(self, obj: Any) -> None:
            raise RuntimeError("DB 挂了")

    primary = _ScriptedClient("primary", [_api_error("上游 502", 502)])
    fallback = _ScriptedClient("fallback", ["备胎答复"])
    router = ModelRouter(primary, fallback, session=_BrokenSession(), trace_id="trace-5")

    assert router.complete(_MESSAGES) == "备胎答复"


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------


def test_stream_text_switches_tier_before_first_increment():
    """吐出任何增量之前失败 → 换下一档从头重来。"""
    primary = _ScriptedClient("primary", [_api_error("上游 502", 502)])
    fallback = _ScriptedClient("fallback", [["备", "胎"]])
    router = ModelRouter(primary, fallback)

    assert list(router.stream_text(_MESSAGES)) == ["备", "胎"]
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1


def test_stream_text_does_not_switch_after_first_increment():
    """已经吐出增量则不再切换(避免内容重复)。"""
    primary = _ScriptedClient(
        "primary", [_StreamThenFail(["半句"], _api_error("上游 502", 502))]
    )
    fallback = _ScriptedClient("fallback", [["备", "胎"]])
    router = ModelRouter(primary, fallback)

    with pytest.raises(LLMApiError):
        list(router.stream_text(_MESSAGES))
    assert fallback.calls == []  # 没有切档,不会重复内容


# ---------------------------------------------------------------------------
# Minimax 备胎客户端(三件套参数化 + 探活打桩)
# ---------------------------------------------------------------------------


def test_minimax_client_reads_fallback_env(monkeypatch):
    """三件套默认从 LLM_FALLBACK_* 环境变量读。"""
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", _PLACEHOLDER_KEY)
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "https://api.minimaxi.com/v1")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "MiniMax-M3.1-Flash")
    # 主用的 key 不应被备胎使用
    monkeypatch.setenv("LLM_API_KEY", "primary-only-key")

    client = MinimaxRealClient()
    try:
        assert client.tier == "fallback"
        assert client.provider == "minimax"
        assert client.base_url == "https://api.minimaxi.com/v1"
        assert client.model == "MiniMax-M3.1-Flash"
        assert client.model_name == "MiniMax-M3.1-Flash"
    finally:
        client.close()


def test_minimax_client_params_override_env(monkeypatch):
    """构造参数优先于环境变量。"""
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "env-key")
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "env-model")

    client = MinimaxRealClient(
        api_key=_PLACEHOLDER_KEY,
        base_url="https://param.example.com/v1/",
        model="param-model",
    )
    try:
        assert client.base_url == "https://param.example.com/v1"  # 去掉结尾斜杠
        assert client.model == "param-model"
    finally:
        client.close()


def test_minimax_client_defaults_without_env(monkeypatch):
    """环境变量缺失时用内置默认网关/模型。"""
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    client = MinimaxRealClient(api_key=_PLACEHOLDER_KEY)
    try:
        assert client.base_url == "https://api.minimaxi.com/v1"
        assert client.model == "MiniMax-M3.1-Flash"
    finally:
        client.close()


def test_minimax_client_missing_key_raises(monkeypatch):
    """缺 key 抛 LLMConfigError,且消息里不出现任何 key。"""
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "primary-only-key")

    with pytest.raises(LLMConfigError) as exc_info:
        MinimaxRealClient()
    message = str(exc_info.value)
    assert "primary-only-key" not in message
    assert "LLM_FALLBACK_API_KEY" in message


class _FakeResponse:
    """假 httpx 响应(不建立连接)。"""

    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


def test_minimax_healthcheck_success_is_stubbed(monkeypatch):
    """healthcheck 打桩:URL / headers / model 正确,返回原文 dict。"""
    client = MinimaxRealClient(
        api_key=_PLACEHOLDER_KEY,
        base_url="https://api.minimaxi.com/v1",
        model="MiniMax-M3.1-Flash",
    )
    captured: dict[str, Any] = {}

    def fake_post(url, *, headers=None, json=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return _FakeResponse(200, {"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr(client._http, "post", fake_post)
    try:
        result = client.healthcheck()
    finally:
        client.close()

    assert captured["url"] == "https://api.minimaxi.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == f"Bearer {_PLACEHOLDER_KEY}"
    assert captured["body"]["model"] == "MiniMax-M3.1-Flash"
    assert captured["body"]["max_tokens"] == 1

    assert result["ok"] is True
    assert result["provider"] == "minimax"
    assert result["model"] == "MiniMax-M3.1-Flash"
    assert result["base_url"] == "https://api.minimaxi.com/v1"
    assert result["status_code"] == 200
    assert result["sample"] == "pong"
    assert result["error"] is None
    assert isinstance(result["latency_ms"], float)


def test_minimax_healthcheck_error_has_no_key(monkeypatch):
    """探活失败:ok=False,错误信息里绝不能出现 key。"""
    client = MinimaxRealClient(api_key=_PLACEHOLDER_KEY)

    def fake_post(url, *, headers=None, json=None, **kwargs):
        return _FakeResponse(500, None, f"boom {_PLACEHOLDER_KEY}")

    monkeypatch.setattr(client._http, "post", fake_post)
    try:
        result = client.healthcheck()
    finally:
        client.close()

    assert result["ok"] is False
    assert result["status_code"] == 500
    assert result["error"]
    assert _PLACEHOLDER_KEY not in json.dumps(result, ensure_ascii=False)


def test_minimax_healthcheck_exception_is_captured(monkeypatch):
    """探活抛异常时返回失败 dict,不让异常冒到调用方。"""
    client = MinimaxRealClient(api_key=_PLACEHOLDER_KEY)

    def fake_post(url, *, headers=None, json=None, **kwargs):
        raise httpx.ConnectError(f"连不上 {_PLACEHOLDER_KEY}")

    monkeypatch.setattr(client._http, "post", fake_post)
    try:
        result = client.healthcheck()
    finally:
        client.close()

    assert result["ok"] is False
    assert result["status_code"] is None
    assert _PLACEHOLDER_KEY not in json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 跨请求熔断持久化(F4 回归)
# ---------------------------------------------------------------------------
#
# 背景:``api/chat.py`` 每轮对话都调一次 ``build_llm()``。原实现每次都新建
# ``ModelRouter`` 与两个 ``CircuitBreaker``,于是"连续 3 次失败开路 / 冷却 30s /
# 半开探针"这套状态机在**真实 HTTP 路径上跨请求永远不生效**——每一轮都从零开始,
# 主用已知挂了也仍要先去撞一次、付满它的超时才降级。
#
# 原有 24 个网关测试都在**同一个 ModelRouter 实例**上连续驱动,验证的是熔断器
# 类本身;走 HTTP 路径的测试又全部把 ``build_llm`` 换成桩。两条路的交集为空,
# 所以这个缺口没有任何测试覆盖。下面两条补的就是这条交集。


def _router_from_env(monkeypatch) -> ModelRouter:
    """按环境变量造真实形态的 ModelRouter(只构造,不出网)。"""
    monkeypatch.setenv("LLM_API_KEY", _PLACEHOLDER_KEY)
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", _PLACEHOLDER_KEY)
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "https://fallback.invalid/v1")
    router = build_llm()
    assert isinstance(router, ModelRouter), "配了备胎就该走 ModelRouter"
    return router


def _replace_clients(router: ModelRouter, primary: Any, fallback: Any) -> None:
    """把真实客户端换成脚本化假客户端(先关原 httpx 连接,不留资源泄漏)。"""
    router.primary.close()
    router.fallback.close()
    router.primary = primary
    router.fallback = fallback


def test_build_llm_shares_breakers_across_calls(monkeypatch):
    """两次 build_llm() 必须共享同一对熔断器,否则熔断状态每轮归零。"""
    first = _router_from_env(monkeypatch)
    second = _router_from_env(monkeypatch)
    try:
        # 本轮只共享熔断器,不把 router 做成单例:attach_trace 每轮改写
        # router 上的 session/trace_id,共享整个 router 会串错追踪归属。
        assert first is not second
        assert first.primary_breaker is second.primary_breaker
        assert first.fallback_breaker is second.fallback_breaker
    finally:
        first.close()
        second.close()


def test_open_breaker_from_previous_turn_skips_primary(monkeypatch):
    """上一轮把主用熔断后,下一轮的全新 router 不该再去撞主用。"""
    first = _router_from_env(monkeypatch)
    _replace_clients(
        first,
        _ScriptedClient("p1", [_api_error("主用 500", 500)] * 3),
        _ScriptedClient("f1", ["备胎答复"] * 3),
    )
    for _ in range(3):
        assert first.complete(_MESSAGES) == "备胎答复"
    assert first.primary_breaker.state == CircuitBreaker.OPEN

    # 下一轮:全新 router + 全新客户端,主用这次"能答"——但熔断开着就不该问它。
    second = _router_from_env(monkeypatch)
    _replace_clients(
        second,
        _ScriptedClient("p2", ["主用答复(熔断期间不该出现)"]),
        _ScriptedClient("f2", ["备胎答复"]),
    )
    try:
        assert second.complete(_MESSAGES) == "备胎答复"
        assert second.primary.calls == [], "熔断开启期间主用不应被调用"
    finally:
        first.close()
        second.close()


def test_shared_breakers_start_closed_for_each_test(monkeypatch):
    """进程级共享熔断器必须逐用例重置,否则用例之间互相污染。

    本条**故意排在上一条(把主用熔断成 open)之后**:没有重置钩子时,
    这里拿到的就是上一条留下的 open 状态,断言必红。
    与 conftest 里 ``semantic_cache.clear()`` / ``security.reset_security_state()``
    同一个道理——进程级单例必须逐用例清。
    """
    router = _router_from_env(monkeypatch)
    try:
        assert router.primary_breaker.state == CircuitBreaker.CLOSED
        assert router.primary_breaker.failure_count == 0
        assert router.fallback_breaker.state == CircuitBreaker.CLOSED
        assert router.fallback_breaker.failure_count == 0
    finally:
        router.close()
