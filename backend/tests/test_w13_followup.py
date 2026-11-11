"""test_w13_followup.py — W13 剩余条目复现/验收测试(ISS-13/14/20/62/63/64/67/69/70 + 接线)。

方法:每条先看缺陷行为,再断言修复后契约;只读 backend/app,不改 data/eval。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import guardrails
from app.api import get_db
from app.cache import CacheEntry, semantic_cache
from app.graph import build as graph_build
from app.graph import nodes, runner
from app.main import app
from app.models import (
    ChatMessage,
    ChatSession,
    Merchant,
    Ticket,
    TicketMessage,
)
from app.tools import registry
from app.tools.events import ToolEvent, write_event
from app.tools.policy import CHECK_TOOL_NAME
from app.tools.results import ToolContext
from app.trace import new_trace_id
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER = "seed-cust-001"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"
ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()
    yield


@pytest.fixture()
def db(engine, seeded):
    with Session(engine) as session:
        yield session


@pytest.fixture()
def client(engine, seeded, tmp_dir, monkeypatch):
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", tmp_dir / "checkpoints.db")
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", tmp_dir / "checkpoints.db")

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()


def _post_chat(client: TestClient, text: str, *, customer_id: str = CUSTOMER) -> dict:
    body = {
        "merchant_id": MERCHANT_ID,
        "customer_id": customer_id,
        "text": text,
        "context": None,
    }
    resp = client.post("/api/c/chat", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# ISS-13:工作台转录按 session_id 穿透 chat_message + ticket_message
# ---------------------------------------------------------------------------


def test_iss13_ticket_detail_includes_pre_handoff_chat(db: Session) -> None:
    """转人工前的 AI 问答必须能从工单详情读到(chat_transcript),且工单消息不重复塞买家前序。"""
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    chat_session = ChatSession(merchant_id=MERCHANT_ID, customer_id="c-iss13")
    db.add(chat_session)
    db.flush()
    db.add_all(
        [
            ChatMessage(
                session_id=chat_session.id,
                role="user",
                content="我的订单什么时候发货?",
                created_at=base,
            ),
            ChatMessage(
                session_id=chat_session.id,
                role="assistant",
                content="您好,订单预计 48 小时内发出。",
                created_at=base + timedelta(seconds=5),
            ),
        ]
    )
    ticket = Ticket(
        id="tk-iss13",
        merchant_id=MERCHANT_ID,
        customer_id="c-iss13",
        session_id=chat_session.id,
        status="queued",
        handoff_reason="user_request",
        summary="买家诉求:什么时候发货",
        created_at=base + timedelta(minutes=1),
    )
    db.add(ticket)
    db.flush()
    db.add(
        TicketMessage(
            ticket_id=ticket.id,
            author_type="user",
            content="我要人工客服",
            is_internal=False,
            created_at=base + timedelta(minutes=1, seconds=3),
        )
    )
    db.commit()

    from app.api import agent as agent_api

    detail = agent_api.ticket_detail("tk-iss13", db=db, identity=_internal_identity())
    assert [item["author_type"] for item in detail["chat_transcript"]] == ["user", "assistant"]
    assert detail["chat_transcript"][0]["content"] == "我的订单什么时候发货?"
    assert detail["chat_transcript"][0]["source"] == "chat"
    # messages 里只有转人工后的工单消息 + assistant 前序(买家前序不进,避免与外部审计重复)
    kinds = [(item["source"], item["author_type"]) for item in detail["messages"]]
    assert ("chat", "user") not in kinds
    assert ("ticket", "user") in kinds
    assert ("chat", "assistant") in kinds


def _internal_identity():
    from app import security

    # internal 身份与 AUTH_MODE=internal 回归兜底一致,不按商户过滤
    return security.Identity(
        sub="tester", role="internal", merchant_id=MERCHANT_ID,
        customer_id=None, service_id="seed-agent-1",
    )


# ---------------------------------------------------------------------------
# ISS-14:转人工摘要规则提取(诉求 + 订单 + 原因),永不空、无英文枚举
# ---------------------------------------------------------------------------


def test_iss14_handoff_summary_extracts_demand_order_reason() -> None:
    summary = nodes._handoff_summary(
        reason="user_request",
        order={"order_id": "seed-order-001", "status": "created", "total_cents": 12900},
        text="  我要转人工  ",
        context={"unresolved_rounds": 2},
    )
    assert "买家诉求:" in summary and "我要转人工" in summary
    assert "订单 seed-order-001" in summary
    assert "待付款" in summary and "created" not in summary
    assert "转接原因:" in summary
    assert str(summary).strip()


def test_iss14_handoff_summary_never_empty_without_text_or_order() -> None:
    summary = nodes._handoff_summary(
        reason="user_request", order=None, text="   ", context={}
    )
    assert summary.strip()
    assert "买家诉求:" in summary


# ---------------------------------------------------------------------------
# ISS-20:handed_off 下“退出人工/转回 AI”必须释放工单并回 active
# ---------------------------------------------------------------------------


def test_iss20_resume_ai_closes_ticket_and_reactivates_session(db: Session) -> None:
    session_row = ChatSession(merchant_id=MERCHANT_ID, customer_id="c-iss20", status="handed_off")
    db.add(session_row)
    db.flush()
    ticket = Ticket(
        id="tk-iss20",
        merchant_id=MERCHANT_ID,
        customer_id="c-iss20",
        session_id=session_row.id,
        status="queued",
        handoff_reason="user_request",
        summary="买家诉求:测试",
    )
    db.add(ticket)
    db.commit()

    assert guardrails.is_resume_ai_request("好的，退出人工，回到机器人")
    result = runner.build_resume_turn(
        session=db, session_row=session_row, trace_id=new_trace_id(), text="退出人工"
    )
    db.commit()
    db.refresh(session_row)
    db.refresh(ticket)
    assert result.session_status == "active"
    assert result.ai_replied is True
    assert "人工" in result.reply
    assert session_row.status == "active"
    assert ticket.status == "closed"
    system_msgs = db.scalars(
        select(TicketMessage).where(TicketMessage.ticket_id == ticket.id)
    ).all()
    assert any(m.author_type == "system" and "转回 AI" in m.content for m in system_msgs)


def test_iss20_normal_handoff_input_still_blocked(db: Session) -> None:
    session_row = ChatSession(merchant_id=MERCHANT_ID, customer_id="c-iss20b", status="handed_off")
    db.add(session_row)
    db.commit()
    assert guardrails.is_resume_ai_request("帮我查一下订单") is False


# ---------------------------------------------------------------------------
# ISS-62/63:ainvoke 独立 Session + 事件 step 落库前 flush
# ---------------------------------------------------------------------------


def test_iss62_ainvoke_does_not_share_caller_session(db: Session, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_impl(name, args, ctx):
        captured["session"] = ctx.session
        captured["name"] = name
        from app.tools.results import ToolResult

        return ToolResult.success({"ok": True}, tool=name, duration_ms=0.0)

    monkeypatch.setattr(registry, "_invoke_impl", fake_impl)
    ctx = ToolContext(
        session=db, merchant_id=MERCHANT_ID, customer_id=CUSTOMER, trace_id=new_trace_id()
    )
    result = asyncio.run(registry.ainvoke("policy__check_return", {}, ctx))
    assert result.ok is True
    worker_session = captured["session"]
    assert worker_session is not db
    assert worker_session.in_transaction() is False  # 已 close,不残留事务/连接


def test_iss63_write_event_flushes_before_next_step(db: Session, monkeypatch) -> None:
    flushes: list[int] = []
    real_flush = db.flush

    def counting_flush(*args, **kwargs):
        flushes.append(1)
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(db, "flush", counting_flush)
    ctx = ToolContext(
        session=db, merchant_id=MERCHANT_ID, customer_id=CUSTOMER, trace_id=new_trace_id()
    )
    registry._write_event(
        ctx, tool_name="policy__check_return", ok=True, error_code=None, payload={"args": {}}
    )
    assert flushes, "ISS-63:_write_event 写完必须 flush,否则同事务内 step 会撞号"


# ---------------------------------------------------------------------------
# ISS-64:策略门必须校验核验入参与本次退款诉求一致
# ---------------------------------------------------------------------------


def test_iss64_policy_gate_rejects_mismatched_reason_or_status(db: Session) -> None:
    from app.tools import refund as refund_tool

    trace_id = new_trace_id()
    write_event(
        db,
        trace_id=trace_id,
        step=0,
        tool_name=CHECK_TOOL_NAME,
        ok=True,
        error_code=None,
        payload={
            "args": {
                "order_status": "delivered",
                "reason": "七天无理由",
                "days_since_delivery": 1,
            }
        },
    )
    db.flush()
    ctx = ToolContext(
        session=db, merchant_id=MERCHANT_ID, customer_id=CUSTOMER, trace_id=trace_id
    )
    order = SimpleNamespace(status="delivered")
    assert refund_tool._policy_gate_passed(ctx, {"reason": "七天无理由"}, order)
    # 退款原因与核验原因不一致 → 拒绝(不能用别的原因核验结果开门)
    assert not refund_tool._policy_gate_passed(ctx, {"reason": "质量问题"}, order)
    # 订单当前状态与核验时状态不一致 → 拒绝
    shipped = SimpleNamespace(status="shipped")
    assert not refund_tool._policy_gate_passed(ctx, {"reason": "七天无理由"}, shipped)


# ---------------------------------------------------------------------------
# ISS-70:eval 报告路径相对 ROOT
# ---------------------------------------------------------------------------


def test_iss70_eval_report_paths_are_relative() -> None:
    import scripts.run_eval as run_eval

    rel = run_eval._rel_path(ROOT / "data" / "eval" / "cases.yaml")
    assert rel == "data/eval/cases.yaml"
    assert not Path(rel).is_absolute()


# ---------------------------------------------------------------------------
# 接线:build_llm → ModelRouter;SemanticCache → chat 读路径
# ---------------------------------------------------------------------------


def test_wiring_build_llm_wraps_router_when_fallback_configured(monkeypatch) -> None:
    from app.llm import build_llm
    from app.llm.router import ModelRouter

    monkeypatch.setenv("LLM_API_KEY", "primary-key")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "https://example.invalid/v1")
    llm = build_llm()
    assert isinstance(llm, ModelRouter)


def test_wiring_semantic_cache_roundtrip_and_guards() -> None:
    semantic_cache.clear()
    payload = {
        "reply": "退货运费由买家承担。",
        "citations": [{"clause_id": "RP-02"}],
        "intent": "policy_qa",
        "confidence": 0.9,
        "intent_reason": "政策问答",
    }
    assert semantic_cache.store("退货的话，来回运费是谁出？", payload, merchant_id=MERCHANT_ID)
    hit = semantic_cache.lookup("退货的话，来回运费是谁出？", merchant_id=MERCHANT_ID)
    assert isinstance(hit, CacheEntry) and hit.reply == payload["reply"]
    # 跨租户不命中
    assert semantic_cache.lookup("退货的话，来回运费是谁出？", merchant_id="other") is None
    # 订单号/写意图不缓存
    assert not semantic_cache.store(
        "我的订单 A202401010001 要退款", payload, merchant_id=MERCHANT_ID
    )
    assert semantic_cache.lookup("我的订单 A202401010001 要退款", merchant_id=MERCHANT_ID) is None


def test_wiring_chat_route_reports_cache_hit(client: TestClient) -> None:
    semantic_cache.clear()
    first = _post_chat(client, "退货的话，来回运费是谁出？")
    assert first.get("cache_hit") is False
    second = _post_chat(client, "退货的话，来回运费是谁出？")
    assert second.get("cache_hit") is True
    assert second["reply"] == first["reply"]
    assert second["usage"]["tokens_in"] == 0
