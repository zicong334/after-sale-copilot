"""W7-2 对话路径 LLM 故障降级测试。

三个 stub 客户端分别模拟批量中断(503)/API 超时/空内容,断言:
- HTTP 200 + ``ok:true`` + ``degraded:true`` + 固定安全话术(不裸抛 500);
- 写一条 ``llm_degraded`` 审计事件(error_type/status_code/model);
- 该轮零业务写:无 tool_events 成功事件、无退款/工单/记忆/确认卡、不落 assistant 消息;
- 正常 FakeLLM 路径不出现 ``degraded``,行为不回归。

说明:stub 与 FakeLLM 都是 MOCK,不发真实网络请求。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import chat as chat_api
from app.api import get_db
from app.graph import build as graph_build
from app.llm import LLMApiError, LLMBatchAbort, LLMEmptyContentError
from app.main import app
from app.memory.models import Memory
from app.models import ChatMessage, ChatSession, RefundRequest, Ticket
from app.tools.events import ConfirmRequest, ToolEvent
from app.trace import trace_events
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER_OTHER = "seed-cust-001"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

#: 固定降级话术(任务书逐字要求,半角逗号)。
DEGRADED_REPLY = "服务暂忙,已记录您的诉求,稍后可重试或转人工。"

#: 不命中快捷路径的普通咨询文本,确保真的会调到 LLM。
NORMAL_QUESTION = "我想问一下这个情况一般该怎么处理比较好"


# ---------------------------------------------------------------------------
# fixtures(与 tests/test_chat_route.py 同源写法)
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    """真实 seed(30 单/8 工单)+ 真实政策库(18 条 knowledge_item)。"""
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()
    yield


@pytest.fixture()
def client(engine, seeded, tmp_dir, monkeypatch):
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", tmp_dir / "checkpoints.db")

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# stub 客户端(MOCK):complete 必抛指定异常
# ---------------------------------------------------------------------------


class FailingLLM:
    """MOCK:每次 ``complete`` 都抛出预设 LLM 异常,用于验证降级路径。"""

    def __init__(self, error: Exception) -> None:
        self.error = error

    @property
    def model_name(self) -> str:
        return "stub-failing-llm (MOCK)"

    def complete(self, messages, schema=None) -> str:  # noqa: ANN001 - 契约同 LLMClient
        raise self.error


def post_chat(client: TestClient, text: str):
    body = {
        "merchant_id": MERCHANT_ID,
        "customer_id": CUSTOMER_OTHER,
        "text": text,
        "context": {},
    }
    return client.post("/api/c/chat", json=body)


def count_rows(engine, model) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def events_of(engine, trace_id: str) -> list[ToolEvent]:
    with Session(engine) as session:
        return trace_events(session, trace_id)


def make_error(kind: str) -> Exception:
    if kind == "batch_abort":
        return LLMBatchAbort("批量中断", status_code=503)
    if kind == "api_error":
        return LLMApiError("timeout")
    if kind == "empty":
        return LLMEmptyContentError("empty")
    raise AssertionError(f"未知的 stub 类型:{kind}")


# ---------------------------------------------------------------------------
# 1) 三类 LLM 故障都降级为 200 + 固定话术
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "error_type", "status_code"),
    [
        ("batch_abort", "LLMBatchAbort", 503),
        ("api_error", "LLMApiError", None),
        ("empty", "LLMEmptyContentError", None),
    ],
)
def test_llm_failure_degrades_without_writes(
    client, engine, monkeypatch, kind, error_type, status_code
) -> None:
    stub = FailingLLM(make_error(kind))
    monkeypatch.setattr(chat_api, "build_llm", lambda: stub)

    before = {
        "refund": count_rows(engine, RefundRequest),
        "ticket": count_rows(engine, Ticket),
        "memory": count_rows(engine, Memory),
        "confirm": count_rows(engine, ConfirmRequest),
    }

    response = post_chat(client, NORMAL_QUESTION)

    # 不裸抛 500:200 + ok + degraded + 固定话术
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["degraded"] is True
    assert body["reply"] == DEGRADED_REPLY
    assert "服务暂忙,已记录您的诉求,稍后可重试或转人工" in body["reply"]
    for claim in ("已退款", "已提交", "已处理", "已为您"):
        assert claim not in body["reply"]
    assert body["intent"] == "degraded"
    assert body["tool_calls"] == []
    assert body["citations"] == []
    assert body["pending_confirm"] is None
    # 基本字段仍在
    assert body["session_id"]
    assert body["trace_id"]
    assert body["session_status"] == "active"

    # 审计事件 llm_degraded:同一 trace,payload 带 error_type/status_code/model
    events = events_of(engine, body["trace_id"])
    degraded_events = [event for event in events if event.tool_name == "llm_degraded"]
    assert len(degraded_events) == 1
    payload = json.loads(degraded_events[0].payload)
    assert payload["error_type"] == error_type
    assert payload["status_code"] == status_code
    assert payload["model"] == "stub-failing-llm (MOCK)"
    assert degraded_events[0].ok is False

    # 零业务写:该 trace 只有降级审计事件,没有 tool_call 成功事件
    assert len(events) == 1

    # 业务表零新增、无待确认卡
    after = {
        "refund": count_rows(engine, RefundRequest),
        "ticket": count_rows(engine, Ticket),
        "memory": count_rows(engine, Memory),
        "confirm": count_rows(engine, ConfirmRequest),
    }
    assert after == before

    # 库内:user 消息保留原文,assistant 消息不落,会话状态不变
    with Session(engine) as session:
        rows = session.scalars(
            select(ChatMessage).where(ChatMessage.trace_id == body["trace_id"])
        ).all()
        roles = [row.role for row in rows]
        session_row = session.get(ChatSession, body["session_id"])
    assert roles == ["user"]
    assert rows[0].content == NORMAL_QUESTION
    assert session_row is not None
    assert session_row.status == "active"


def test_sse_stream_also_degrades(client, monkeypatch) -> None:
    """SSE 端点复用 _run_turn,降级帧带 degraded 标记与固定话术。"""
    monkeypatch.setattr(chat_api, "build_llm", lambda: FailingLLM(LLMApiError("timeout")))
    payload = {
        "merchant_id": MERCHANT_ID,
        "customer_id": CUSTOMER_OTHER,
        "text": NORMAL_QUESTION,
        "context": {},
    }
    response = client.post("/api/c/chat/stream", json=payload)

    assert response.status_code == 200
    text = response.text
    assert "event: done" in text
    done_raw = [line for line in text.splitlines() if line.startswith("data: ")][-1]
    done = json.loads(done_raw[len("data: "):])
    assert done["degraded"] is True
    assert DEGRADED_REPLY in text


# ---------------------------------------------------------------------------
# 2) 正常路径不回归
# ---------------------------------------------------------------------------


def test_normal_path_has_no_degraded_flag(client, engine) -> None:
    """默认 FakeLLM 正常回答:无 degraded 键、无降级事件、assistant 消息照常落。"""
    response = post_chat(client, NORMAL_QUESTION)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "degraded" not in body
    assert body["intent"] != "degraded"
    assert body["reply"]
    events = events_of(engine, body["trace_id"])
    assert all(event.tool_name != "llm_degraded" for event in events)

    with Session(engine) as session:
        roles = [
            row.role
            for row in session.scalars(
                select(ChatMessage).where(ChatMessage.trace_id == body["trace_id"])
            ).all()
        ]
    assert roles == ["user", "assistant"]
