"""test_session_scope.py — W5-FIX R1/R2/R3/R4 回归。

覆盖:
1. F1:handoff 建单写入 ``ticket.session_id``;会话摘要只认本会话工单;
   同客户历史 closed 工单不再污染新会话(不误禁用输入框);无 session_id 的
   历史工单不影响新会话(兼容)。
2. F1:``/api/c/handoff/summary`` 可选 ``session_id`` 参数;不传保持旧行为;
   两者都缺参数时 400。
3. F4:转人工后连发 3 条留言仍 append 进本会话工单(author=user);
   坐席回复后买家摘要可见人工消息;关单后摘要 ticket_closed=True;
   关单后再发消息不报错且不追加到已关闭工单。
4. F3:GET / 与 *.html 带 ``Cache-Control: no-store``;静态资源仍带 etag;
   ``/favicon.ico`` 200(image/svg+xml)。
5. F2:chat.html ↔ agent.html 双向入口;app.js 状态只由本会话工单驱动。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import get_db
from app.graph import build as graph_build
from app.main import app
from app.models import ChatSession, Ticket, TicketMessage
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER = "seed-cust-001"
HANDOFF_TEXT = "我不跟你聊了,直接给我转人工客服"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


# ---------------------------------------------------------------------------
# fixtures(与 test_handoff_flow.py 同构)
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


def post_chat(client: TestClient, text: str, *, customer_id: str = CUSTOMER,
              session_id: str | None = None) -> dict:
    body: dict = {"merchant_id": MERCHANT_ID, "customer_id": customer_id, "text": text}
    if session_id:
        body["session_id"] = session_id
    resp = client.post("/api/c/chat", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def handoff_and_confirm(client: TestClient) -> dict:
    """转人工 → 确认建单;返回确认响应 + session_id。"""
    first = post_chat(client, HANDOFF_TEXT)
    token = first["pending_confirm"]["confirm_token"]
    resp = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": first["session_id"]})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    out["session_id"] = first["session_id"]
    return out


# ---------------------------------------------------------------------------
# F1:工单归属到会话
# ---------------------------------------------------------------------------


def test_new_ticket_records_session_id(client, engine):
    out = handoff_and_confirm(client)
    session_id = out["session_id"]
    ticket_id = out["receipt"]["ticket_id"]

    with Session(engine) as db:
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.session_id == session_id, "R1:新工单必须记录 session_id"
        assert ticket.status == "queued"


def test_legacy_closed_ticket_does_not_affect_new_session(client, engine):
    """同客户历史 closed 工单(旧版无 session_id)不得把新会话判成已结束。"""
    with Session(engine) as db:
        db.add(
            Ticket(
                id="tk-legacy-closed",
                merchant_id=MERCHANT_ID,
                customer_id=CUSTOMER,
                session_id=None,
                status="closed",
            )
        )
        db.commit()

    out = handoff_and_confirm(client)
    session_id = out["session_id"]
    ticket_id = out["receipt"]["ticket_id"]

    resp = client.get(f"/api/c/handoff/sessions/{session_id}/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket"] is not None
    assert body["ticket"]["ticket_id"] == ticket_id
    assert body["ticket"]["session_id"] == session_id
    assert body["ticket_status"] == "queued"
    assert body["ticket_closed"] is False, "R1:历史 closed 工单不得污染新会话"


def test_legacy_sessionless_ticket_not_attributed_to_new_session(client, engine):
    """旧工单(session_id 为 NULL)不再按客户启发式归属到新会话。"""
    with Session(engine) as db:
        db.add(ChatSession(id="s-legacy-null", merchant_id=MERCHANT_ID, customer_id=CUSTOMER))
        db.add(
            Ticket(
                id="tk-legacy-null",
                merchant_id=MERCHANT_ID,
                customer_id=CUSTOMER,
                session_id=None,
                status="queued",
            )
        )
        db.commit()

    resp = client.get("/api/c/handoff/sessions/s-legacy-null/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket"] is None, "无 session_id 的历史工单不得回退给任意会话"
    assert body["ticket_closed"] is False
    assert body["ticket_status"] is None


# ---------------------------------------------------------------------------
# F1:summary 端点可选 session_id + 旧行为兼容
# ---------------------------------------------------------------------------


def test_summary_endpoint_by_session_id_matches_path_endpoint(client):
    out = handoff_and_confirm(client)
    session_id = out["session_id"]
    ticket_id = out["receipt"]["ticket_id"]

    by_query = client.get("/api/c/handoff/summary", params={"session_id": session_id})
    assert by_query.status_code == 200, by_query.text
    by_path = client.get(f"/api/c/handoff/sessions/{session_id}/summary")
    assert by_path.status_code == 200, by_path.text
    assert by_query.json() == by_path.json()
    assert by_query.json()["ticket"]["ticket_id"] == ticket_id


def test_summary_endpoint_legacy_behavior_without_session_id(client):
    out = handoff_and_confirm(client)
    ticket_id = out["receipt"]["ticket_id"]

    legacy = client.get(
        "/api/c/handoff/summary",
        params={"merchant_id": MERCHANT_ID, "customer_id": CUSTOMER},
    )
    assert legacy.status_code == 200, legacy.text
    body = legacy.json()
    assert body["ok"] is True
    assert body["ticket"] is not None
    assert body["ticket"]["ticket_id"] == ticket_id
    assert "旧行为兼容" in body["ticket_link"]


def test_summary_endpoint_requires_something(client):
    resp = client.get("/api/c/handoff/summary")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_summary_endpoint_unknown_session_404(client):
    resp = client.get("/api/c/handoff/summary", params={"session_id": "no-such-session"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# F4:留言追加 / 坐席回复 / 关单作用域
# ---------------------------------------------------------------------------


def test_blocked_messages_append_then_close_scopes_to_session(client, engine):
    out = handoff_and_confirm(client)
    session_id = out["session_id"]
    ticket_id = out["receipt"]["ticket_id"]

    # 转人工后连发 3 条留言:都追加到本会话工单(author=user),不是新会话
    for idx in range(3):
        blocked = post_chat(client, f"补充说明 {idx}", session_id=session_id)
        assert blocked["ai_replied"] is False
        assert blocked["ack_ticket_id"] == ticket_id, "R1:留言必须追加到本会话工单"

    with Session(engine) as db:
        rows = db.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
        ).all()
        assert [row.author_type for row in rows] == ["user", "user", "user"]
        assert [row.content for row in rows] == ["补充说明 0", "补充说明 1", "补充说明 2"]

    # 工作台领单 + 回复
    claim = client.post(
        f"/api/b/tickets/{ticket_id}/claim", json={"service_id": "seed-agent-1"},
    )
    assert claim.status_code == 200, claim.text
    reply_text = "您好,退款我来跟进,请稍候"
    reply = client.post(
        f"/api/b/tickets/{ticket_id}/messages",
        json={"service_id": "seed-agent-1", "content": reply_text},
    )
    assert reply.status_code == 200, reply.text

    summary = client.get("/api/c/handoff/summary", params={"session_id": session_id})
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["ticket_status"] == "claimed"
    assert body["ticket_closed"] is False, "坐席回复后工单仍开着,composer 必须可用"
    agent_msgs = [
        item for item in body["ticket_messages"] if item["author_type"] == "agent"
    ]
    assert any(item["content"] == reply_text for item in agent_msgs), "买家页应见人工气泡"

    # 关单:只有本会话摘要变 closed
    closed = client.post(
        f"/api/b/tickets/{ticket_id}/close", json={"service_id": "seed-agent-1"},
    )
    assert closed.status_code == 200, closed.text
    after_close = client.get("/api/c/handoff/summary", params={"session_id": session_id})
    assert after_close.status_code == 200, after_close.text
    assert after_close.json()["ticket_closed"] is True
    assert after_close.json()["ticket_status"] == "closed"

    # 关单后买家再发消息:AI 仍静默、不追加到已关闭工单、不抛 500
    after = post_chat(client, "还在吗", session_id=session_id)
    assert after["ai_replied"] is False
    assert after["ack_ticket_id"] is None

    with Session(engine) as db:
        count = len(
            db.scalars(
                select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)
            ).all()
        )
        assert count == 4, "已关闭工单不得再追加买家消息(3 user + 1 agent)"


# ---------------------------------------------------------------------------
# F3:html no-store + favicon + 静态资源 etag
# ---------------------------------------------------------------------------


def test_html_responses_are_no_store(client):
    for path in ("/", "/chat.html", "/agent.html"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == "no-store", path


def test_static_assets_keep_etag(client):
    resp = client.get("/app.js")
    assert resp.status_code == 200
    assert resp.headers.get("etag"), "CSS/JS 保留 etag 正常缓存"
    assert resp.headers.get("cache-control") != "no-store"


def test_favicon_no_longer_404(client):
    for path in ("/favicon.ico", "/favicon.svg"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} 不得再 404"
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in resp.text


# ---------------------------------------------------------------------------
# F2:双向入口 + 前端状态机不再按客户取最近工单
# ---------------------------------------------------------------------------


def test_frontend_has_cross_links(client):
    chat = client.get("/chat.html")
    assert chat.status_code == 200
    assert 'href="/agent.html"' in chat.text, "聊天页必须有客服工作台入口"

    agent = client.get("/agent.html")
    assert agent.status_code == 200
    assert 'href="/chat.html"' in agent.text, "工作台必须有返回买家聊天入口"


def test_app_js_uses_session_scoped_summary():
    script = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
    assert "/api/c/handoff/summary?session_id=" in script, "必须走会话作用域摘要"
    assert "/api/c/handoff/sessions/" not in script, "不得再调用按客户推断的路径端点"
    assert "留言将追加到人工工单" in script, "转人工后 composer 应保持可用并提示追加"
    assert "handoffBar" in script, "转人工后应有顶部轻量提示条"

    chat_html = (FRONTEND_DIR / "chat.html").read_text(encoding="utf-8")
    assert 'id="handoffBar"' in chat_html
