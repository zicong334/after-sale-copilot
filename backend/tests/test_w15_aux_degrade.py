"""test_w15_aux_degrade.py — W15 U7 辅助模块降级壳 + U6 轮询瘦身的行为验收。

覆盖(6 条):
1. 语义缓存 lookup 抛错 → chat 主链路仍 200 且有回复(绝不 500);
2. 语义缓存 lookup 抛错 → 落一条 ``aux_degraded`` trace 事件;
3. suggest 条款检索抛错 → 仍返回建议回复(citations 退化为空);
4. suggest 条款检索抛错 → 落一条 ``aux_degraded`` trace 事件;
5. SLA 扫描单条告警事件写失败 → 扫描不外溢异常、会话仍可用;
6. summary ``view=brief`` → 不含任何消息正文(full 视图形状不变)。

只读 backend/app;本文件是 W15 新增验收产物,不改动既有实现。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import agent as agent_api
from app.api import chat as chat_api
from app.api import get_db
from app.graph import build as graph_build
from app.main import app
from app.models import ChatSession, Merchant, Ticket, TicketMessage
from app.sla import scan_breaches
from app.tools.events import ToolEvent
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER = "seed-cust-001"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


# ---------------------------------------------------------------------------
# fixtures(与 test_handoff_flow.py / test_agent_workbench.py 一致)
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()


@pytest.fixture()
def ckpt(tmp_dir, monkeypatch) -> Path:
    path = tmp_dir / "checkpoints.db"
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", path)
    return path


@pytest.fixture()
def client(engine, seeded, ckpt):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _aux_degraded_events(engine) -> list[ToolEvent]:
    """落库的 aux_degraded 事件(降级壳的可审计痕迹)。"""
    with Session(engine) as db:
        return list(
            db.scalars(
                select(ToolEvent).where(ToolEvent.tool_name == "aux_degraded")
            ).all()
        )


def _add_ticket(engine, ticket_id: str, *, merchant: str = MERCHANT_ID,
                session_id: str | None = None, status: str = "claimed",
                created_at: datetime | None = None) -> None:
    with Session(engine) as db:
        if db.get(Merchant, merchant) is None:
            db.add(Merchant(id=merchant, name="W15 降级测试店"))
            db.flush()
        db.add(
            Ticket(
                id=ticket_id,
                merchant_id=merchant,
                customer_id=CUSTOMER,
                session_id=session_id,
                status=status,
                claimed_by=("seed-agent-1" if status != "queued" else None),
                handoff_reason="买家要求人工",
                summary="买家反馈商品质量问题,要求退款",
                created_at=created_at or datetime.now(timezone.utc),
            )
        )
        db.add(
            TicketMessage(
                ticket_id=ticket_id,
                author_type="user",
                content="收到的锅是坏的,我要退款,这是质量问题",
            )
        )
        db.commit()


# ---------------------------------------------------------------------------
# U7-1/U7-2 语义缓存降级壳
# ---------------------------------------------------------------------------


def test_w15_semantic_cache_lookup_failure_keeps_chat_ok(client, engine, monkeypatch):
    """语义缓存读路径抛错 → 主链路照常回答(绝不 500)。"""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("语义缓存后端不可用")

    monkeypatch.setattr(chat_api.semantic_cache, "lookup", _boom)
    resp = client.post(
        "/api/c/chat",
        json={
            "merchant_id": MERCHANT_ID,
            "customer_id": CUSTOMER,
            "text": "退货的话,来回运费是谁出?",
            "context": None,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True
    assert str(body.get("reply") or "").strip(), "降级后仍应给出回复"


def test_w15_semantic_cache_failure_records_aux_degraded(client, engine, monkeypatch):
    """降级要留痕:tool_events 出现 aux_degraded 事件。"""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("语义缓存后端不可用")

    monkeypatch.setattr(chat_api.semantic_cache, "lookup", _boom)
    resp = client.post(
        "/api/c/chat",
        json={
            "merchant_id": MERCHANT_ID,
            "customer_id": CUSTOMER,
            "text": "退货的话,来回运费是谁出?",
            "context": None,
        },
    )
    assert resp.status_code == 200, resp.text
    events = _aux_degraded_events(engine)
    assert events, "降级后应落一条 aux_degraded 事件"
    assert any(
        "semantic_cache" in str(event.payload or "") for event in events
    ), f"aux_degraded 事件应标明来源: {[e.payload for e in events]}"


# ---------------------------------------------------------------------------
# U7-3/U7-4 建议回复条款检索降级壳
# ---------------------------------------------------------------------------


def test_w15_suggest_citations_failure_still_returns_reply(client, engine, monkeypatch):
    """条款检索抛错 → 建议回复照常生成(退化为无条款引用)。"""
    ticket_id = "tk-w15-suggest-degrade"
    _add_ticket(engine, ticket_id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("条款索引不可用")

    monkeypatch.setattr(agent_api, "_suggest_citations", _boom)
    resp = client.post(f"/api/b/tickets/{ticket_id}/suggest")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert str(data.get("suggested_reply") or "").strip(), "降级后仍应给出建议回复"
    assert data.get("citations") == [], "降级后不应残留条款引用"


def test_w15_suggest_failure_records_aux_degraded(client, engine, monkeypatch):
    """条款检索降级 → 落一条 aux_degraded 事件。"""
    ticket_id = "tk-w15-suggest-degrade-evt"
    _add_ticket(engine, ticket_id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("条款索引不可用")

    monkeypatch.setattr(agent_api, "_suggest_citations", _boom)
    resp = client.post(f"/api/b/tickets/{ticket_id}/suggest")
    assert resp.status_code == 200, resp.text
    events = _aux_degraded_events(engine)
    assert events, "降级后应落一条 aux_degraded 事件"
    assert any(
        "suggest" in str(event.payload or "") for event in events
    ), f"aux_degraded 事件应标明来源: {[e.payload for e in events]}"


# ---------------------------------------------------------------------------
# U7-5 SLA 扫描降级壳
# ---------------------------------------------------------------------------


def test_w15_sla_scan_failure_does_not_escape(engine, monkeypatch):
    """单条告警事件写失败 → 扫描不抛异常,会话仍可用。"""
    created = datetime.now(timezone.utc) - timedelta(hours=2)
    _add_ticket(engine, "tk-w15-sla-degrade", status="queued", created_at=created)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("trace 写入失败")

    monkeypatch.setattr("app.sla.record_event", _boom)
    with Session(engine) as db:
        result = scan_breaches(db, now=datetime.now(timezone.utc), threshold_minutes=30)
        assert isinstance(result, list)
        # 降级后会话仍可用(没有 PendingRollbackError)。
        db.execute(select(Ticket).where(Ticket.id == "tk-w15-sla-degrade"))
    events = _aux_degraded_events(engine)
    assert events, "SLA 扫描降级应落一条 aux_degraded 事件"


# ---------------------------------------------------------------------------
# U6 轮询瘦身
# ---------------------------------------------------------------------------


def test_w15_summary_brief_view_has_no_transcript(client, engine):
    """view=brief 只回状态与计数,不含任何消息正文。"""
    session_id = "cs-w15-brief"
    ticket_id = "tk-w15-brief"
    with Session(engine) as db:
        db.add(
            ChatSession(
                id=session_id,
                merchant_id=MERCHANT_ID,
                customer_id=CUSTOMER,
                status="active",
            )
        )
        db.commit()
    _add_ticket(engine, ticket_id, session_id=session_id)

    resp = client.get(f"/api/c/handoff/sessions/{session_id}/summary", params={"view": "brief"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["view"] == "brief"
    assert "ticket_messages" not in body, "brief 视图不得下发工单消息全文"
    assert "messages" not in body, "brief 视图不得下发会话消息全文"
    assert "message_count" in body
    assert "ticket_status" in body
    # 极简视图里只能出现短码级字段,不含消息正文内容。
    assert "收到的锅是坏的" not in resp.text


def test_w15_summary_full_view_keeps_transcript(client, engine):
    """默认 full 视图形状不变(兼容既有调用方与既有回归)。"""
    session_id = "cs-w15-full"
    ticket_id = "tk-w15-full"
    with Session(engine) as db:
        db.add(
            ChatSession(
                id=session_id,
                merchant_id=MERCHANT_ID,
                customer_id=CUSTOMER,
                status="active",
            )
        )
        db.commit()
    _add_ticket(engine, ticket_id, session_id=session_id)

    resp = client.get(f"/api/c/handoff/sessions/{session_id}/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "ticket_messages" in body
    assert "messages" in body


def test_w15_summary_invalid_view_rejected(client, engine):
    """非法 view 参数 → 400 VALIDATION_ERROR(不静默降级成 full)。"""
    resp = client.get("/api/c/handoff/summary", params={"session_id": "cs-x", "view": "bogus"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
