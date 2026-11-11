"""W12-1 /metrics 测试:Prometheus 文本格式合规 + 计数随数据增长。

覆盖口径(全部基于本地临时 SQLite,不发真实 API 请求):
1. 端点返回 200 / Prometheus 0.0.4 content-type,行格式合规(HELP/TYPE/样本行成对);
2. 工具调用计数随事件增长(清缓存后重新采集),工具错误按 error_code 分桶;
3. 节点延迟分位(p50/p95)按同 trace 相邻事件时间差推导;
4. 护栏拦截分布(prompt_probe/injection/pii/risky 四条);
5. LLM 路由事件 + 熔断器状态 gauge;
6. 队列深度按 job_queue.status;
7. 语义缓存命中率等于进程内缓存 stats();
8. 暴露策略:关闭 → 404、非本机 → 403、显式允许远端 → 200。
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import observability
from app.api import get_db
from app.cache import semantic_cache
from app.db import utc_now
from app.main import app
from app.models.jobs import Job
from app.tools.events import ToolEvent, write_event
from app.trace import record_event

#: Prometheus 样本行:``name{labels} value``(names 允许 ``_`` 与 ``:``)。
_SAMPLE_RE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*"
    r"(\{[^{}]*\})?"
    r" (?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|NaN|\+Inf|-Inf)$"
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(engine, monkeypatch):
    """把 get_db 指向测试 engine;关闭后台刷新线程(避免守护线程跨用例写缓存)。"""
    monkeypatch.setenv("METRICS_BACKGROUND_REFRESH", "0")
    monkeypatch.delenv("METRICS_ENABLED", raising=False)
    monkeypatch.delenv("METRICS_ALLOW_REMOTE", raising=False)
    observability.CACHE.clear()

    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()
    observability.CACHE.clear()


# ---------------------------------------------------------------------------
# 解析 helper
# ---------------------------------------------------------------------------


def _scrape(client: TestClient) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    # 每次抓取都从测试库现算,避免进程级缓存掩盖计数变化。
    observability.CACHE.clear()
    return response.text


def _samples(text: str) -> list[tuple[str, dict[str, str], float]]:
    """把文本解析成 (指标名, 标签 dict, 数值) 列表(跳过 HELP/TYPE/空行)。"""
    parsed: list[tuple[str, dict[str, str], float]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^{}]*\})? (.+)$", line)
        assert match, f"样本行不合规: {line!r}"
        name, raw_labels, raw_value = match.group(1), match.group(2), match.group(3)
        labels: dict[str, str] = {}
        if raw_labels:
            for item in re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', raw_labels):
                labels[item[0]] = item[1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        parsed.append((name, labels, float(raw_value)))
    return parsed


def _value(
    text: str,
    name: str,
    **labels: str,
) -> float | None:
    """取指定指标 + 标签的值;匹配不到返回 None。"""
    target = {key: str(value) for key, value in labels.items()}
    for metric_name, metric_labels, value in _samples(text):
        if metric_name != name:
            continue
        if all(metric_labels.get(key) == expected for key, expected in target.items()):
            return value
    return None


def _labels_match(text: str, name: str, **labels: str) -> list[float]:
    target = {key: str(value) for key, value in labels.items()}
    return [
        value
        for metric_name, metric_labels, value in _samples(text)
        if metric_name == name
        and all(metric_labels.get(key) == expected for key, expected in target.items())
    ]


# ---------------------------------------------------------------------------
# 1. 格式合规
# ---------------------------------------------------------------------------


def test_metrics_scrape_format_is_prometheus_text(client: TestClient) -> None:
    text = _scrape(client)
    lines = text.splitlines()
    assert lines, "指标文本不能为空"

    documented: dict[str, str] = {}
    for index, line in enumerate(lines):
        if line.startswith("# HELP "):
            help_parts = line.split()
            name = help_parts[2]
            next_line = lines[index + 1]
            assert next_line.startswith("# TYPE "), f"{name} 的 HELP 后缺 TYPE"
            type_parts = next_line.split()
            assert type_parts[2] == name, f"{name} 的 TYPE 行指标名不一致"
            documented[name] = type_parts[3]
        elif line.startswith("# TYPE "):
            type_parts = line.split()
            name = type_parts[2]
            assert name in documented, f"{name} 的 TYPE 缺对应 HELP"
            assert documented[name] in {"counter", "gauge", "histogram", "summary"}
        elif line and not line.startswith("#"):
            assert _SAMPLE_RE.match(line), f"样本行不合规: {line!r}"

    for required in (
        "copilot_node_latency_ms",
        "copilot_agent_tokens_total",
        "copilot_guardrail_blocks_total",
        "copilot_tool_events_total",
        "copilot_tool_errors_total",
        "copilot_circuit_breaker_state",
        "copilot_job_queue_depth",
        "copilot_semantic_cache_hit_ratio",
    ):
        assert required in documented, f"缺少必需指标 {required}"


# ---------------------------------------------------------------------------
# 2. 工具计数随事件增长
# ---------------------------------------------------------------------------


def test_tool_counts_grow_with_events(client: TestClient, session: Session) -> None:
    for _ in range(2):
        write_event(
            session,
            trace_id="trace-grow",
            step=0,
            tool_name="order__get",
            ok=True,
            error_code=None,
            payload={"args": {"order_id": "o-1"}},
        )
    session.commit()

    first = _scrape(client)
    assert _value(first, "copilot_tool_events_total", tool="order__get", result="ok") == 2

    write_event(
        session,
        trace_id="trace-grow",
        step=1,
        tool_name="order__get",
        ok=False,
        error_code="NOT_FOUND",
        payload={"args": {"order_id": "o-404"}},
    )
    session.commit()

    second = _scrape(client)
    assert _value(second, "copilot_tool_events_total", tool="order__get", result="ok") == 2
    assert _value(second, "copilot_tool_events_total", tool="order__get", result="error") == 1
    assert (
        _value(second, "copilot_tool_errors_total", tool="order__get", error_code="NOT_FOUND")
        == 1
    )
    assert _value(second, "copilot_tool_error_ratio", tool="order__get") == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# 3. 节点延迟分位
# ---------------------------------------------------------------------------


def test_node_latency_percentiles_by_graph_node(client: TestClient, session: Session) -> None:
    base = utc_now()
    # 同一 trace 内三条事件:相邻时间差 30ms / 70ms,归因到后一条事件的 tool_name。
    session.add(
        ToolEvent(
            trace_id="trace-latency",
            step=1,
            tool_name="router__classify",
            ok=True,
            error_code=None,
            payload="{}",
            ts=base,
        )
    )
    session.add(
        ToolEvent(
            trace_id="trace-latency",
            step=2,
            tool_name="policy__search",
            ok=True,
            error_code=None,
            payload="{}",
            ts=base + timedelta(milliseconds=30),
        )
    )
    session.add(
        ToolEvent(
            trace_id="trace-latency",
            step=3,
            tool_name="policy__search",
            ok=True,
            error_code=None,
            payload="{}",
            ts=base + timedelta(milliseconds=100),
        )
    )
    session.commit()

    text = _scrape(client)
    # SQLite 往返 + float timestamp 有微秒级误差,分位值按 0.5ms 容差断言。
    assert _value(text, "copilot_node_latency_ms", node="policy__search", quantile="0.5") == pytest.approx(30, abs=0.5)
    assert _value(text, "copilot_node_latency_ms", node="policy__search", quantile="0.95") == pytest.approx(70, abs=0.5)
    assert _value(text, "copilot_node_latency_samples", node="policy__search") == 2
    # 桶名 = tool_events.tool_name(编排事件取事件名),未发生相邻事件的桶不输出。
    assert _labels_match(text, "copilot_node_latency_ms", node="router__classify") == []


# ---------------------------------------------------------------------------
# 4. 护栏拦截分布
# ---------------------------------------------------------------------------


def test_guardrail_block_distribution(client: TestClient, session: Session) -> None:
    record_event(
        session,
        trace_id="trace-guard",
        event="graph__prompt_probe_block",
        payload={"flags": ["injection_suspect", "privacy_block"]},
    )
    session.add(
        ToolEvent(
            trace_id="trace-guard",
            step=1,
            tool_name="handoff__create_ticket",
            ok=True,
            error_code=None,
            payload='{"args": {"reason": "complaint", "summary": "客户投诉"}}',
        )
    )
    session.commit()

    text = _scrape(client)
    assert _value(text, "copilot_guardrail_blocks_total", guardrail="prompt_probe") == 1
    assert _value(text, "copilot_guardrail_blocks_total", guardrail="injection") == 1
    assert _value(text, "copilot_guardrail_blocks_total", guardrail="pii") == 1
    assert _value(text, "copilot_guardrail_blocks_total", guardrail="risky") == 1


# ---------------------------------------------------------------------------
# 5. LLM 路由事件 + 熔断器状态
# ---------------------------------------------------------------------------


def test_route_events_and_breaker_state(client: TestClient, session: Session) -> None:
    record_event(
        session,
        trace_id="trace-route",
        event="router_failover",
        payload={"from": "primary", "to": "fallback", "breaker_state": "open", "reason": "timeout"},
        ok=False,
        error_code="TIMEOUT",
    )
    session.commit()

    text = _scrape(client)
    assert _value(text, "copilot_llm_route_events_total", tier="fallback") == 1
    assert _value(text, "copilot_circuit_breaker_state", tier="fallback", state="open") == 1
    assert _value(text, "copilot_circuit_breaker_state", tier="fallback", state="closed") == 0


# ---------------------------------------------------------------------------
# 6. 队列深度
# ---------------------------------------------------------------------------


def test_job_queue_depth_by_status(client: TestClient, session: Session) -> None:
    session.add_all(
        [
            Job(kind="memory_extract", status="pending"),
            Job(kind="memory_extract", status="pending"),
            Job(kind="turn_audit", status="running"),
        ]
    )
    session.commit()

    text = _scrape(client)
    assert _value(text, "copilot_job_queue_depth", status="pending") == 2
    assert _value(text, "copilot_job_queue_depth", status="running") == 1


# ---------------------------------------------------------------------------
# 7. 缓存命中率
# ---------------------------------------------------------------------------


def test_semantic_cache_ratio_matches_stats(client: TestClient) -> None:
    payload = {
        "reply": "48 小时内可退",
        "citations": [],
        "intent": "policy_question",
        "confidence": 0.9,
        "intent_reason": "政策咨询",
    }
    assert semantic_cache.store("退货政策是什么", payload, merchant_id="m-1") is True
    assert semantic_cache.lookup("退货政策是什么", merchant_id="m-1") is not None
    assert semantic_cache.lookup("发货多久到", merchant_id="m-1") is None

    stats = semantic_cache.stats()
    text = _scrape(client)
    assert _value(text, "copilot_semantic_cache_hits_total") == stats["hits"]
    assert _value(text, "copilot_semantic_cache_misses_total") == stats["misses"]
    assert _value(text, "copilot_semantic_cache_entries") == stats["size"]
    assert (stats["hits"] + stats["misses"]) > 0


# ---------------------------------------------------------------------------
# 8. 暴露策略
# ---------------------------------------------------------------------------


def test_metrics_disabled_returns_404(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("METRICS_ENABLED", "0")
    response = client.get("/metrics")
    assert response.status_code == 404


def test_metrics_rejects_remote_client_by_default(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(observability, "client_is_local", lambda _host: False)
    response = client.get("/metrics")
    assert response.status_code == 403


def test_metrics_allows_remote_when_explicitly_enabled(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(observability, "client_is_local", lambda _host: False)
    monkeypatch.setenv("METRICS_ALLOW_REMOTE", "1")
    response = client.get("/metrics")
    assert response.status_code == 200


def test_client_is_local_host_set() -> None:
    assert observability.client_is_local("127.0.0.1") is True
    assert observability.client_is_local("::1") is True
    assert observability.client_is_local("testclient") is True
    assert observability.client_is_local("10.0.0.9") is False
    assert observability.client_is_local(None) is False
