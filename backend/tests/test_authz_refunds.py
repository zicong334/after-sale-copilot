"""A1 越权回归(2026-11-16 审计发现,见 docs/审计发现_20261116.md)。

缺陷:W13-ISS-60 把**认证**扩到全部路由(非公开路径一律要 Bearer),但没把**授权**
扩过去——``/orders`` ``/tickets`` ``/refunds`` 是 ``ROLE_ANY``,且这三组 router 内部
只比 ``merchant_id``、不比角色、不比 customer。于是一个买家 token 就能:

- 把**别人**的订单推成 delivered(``POST /orders/{id}/deliver``)
- 给别人的订单建全额退款单 → **自己审核** → **自己打款完成**
- 冒充坐席 ``service_id`` 领单并关单

这直接击穿 PRD §4 第一红线「退款永远只是创建申请单 + 人工审核,永不自动打款」。
对话链路有确认门挡住,REST 直调完全绕过。

本文件全部用 **enforce 生产模式 + 真实 JWT**,断言:
1. 坐席专属写操作对 buyer → 403 FORBIDDEN;
2. 买家只能操作**自己**的订单/退款单(他人 → 404,按"不存在"处理不泄露存在性);
3. ``decided_by`` / ``service_id`` 一律从 token 取,请求体值无身份效力;
4. 正常坐席链路不受影响(正向用例,防守卫过度)。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.graph import build as graph_build
from app.main import app
from app.models import Order, RefundRequest, Ticket
from scripts.seed import seed
from scripts.load_policies import parse_policies, upsert_policies

DEMO_PASSWORD = "demo1234"
MERCHANT = "seed-merchant-1"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


@pytest.fixture()
def seeded(engine):
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()
    yield


@pytest.fixture()
def client(engine, seeded, tmp_dir, monkeypatch):
    """enforce 模式 TestClient(与 test_auth.py 同一形态)。"""
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_ENFORCE)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", False)
    security.reset_security_state()
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", tmp_dir / "checkpoints.db")

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    c = TestClient(app)
    yield c
    c.close()
    app.dependency_overrides.clear()


def bearer(client: TestClient, username: str) -> dict[str, str]:
    r = client.post("/auth/login", json={"username": username, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _order(engine, *, status: str, customer: str | None = None) -> Order:
    with Session(engine) as s:
        q = select(Order).where(Order.status == status)
        if customer is not None:
            q = q.where(Order.customer_id == customer)
        row = s.scalars(q).first()
        assert row is not None, f"seed 里没有 status={status} customer={customer} 的订单"
        s.expunge(row)
        return row


def _create_refund(client, headers, order, *, reason="七天无理由"):
    r = client.post("/refunds", headers=headers, json={
        "merchant_id": MERCHANT,
        "order_id": order.id,
        "amount_cents": order.total_cents,
        "reason": reason,
        "idempotency_key": f"authz-{uuid.uuid4().hex}",
    })
    return r


# ---------------------------------------------------------------------------
# 1) 坐席专属写操作:buyer → 403
# ---------------------------------------------------------------------------


def test_buyer_cannot_deliver_order(client, engine):
    """买家不得推订单签收(坐席/系统操作)。"""
    order = _order(engine, status="shipped")
    h = bearer(client, "buyer_demo")
    r = client.post(f"/orders/{order.id}/deliver", headers=h)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_buyer_cannot_ship_or_pay_order(client, engine):
    """发货与支付同样是坐席/系统侧写操作。"""
    h = bearer(client, "buyer_demo")
    paid = _order(engine, status="paid")
    assert client.post(f"/orders/{paid.id}/ship", headers=h).status_code == 403
    created = _order(engine, status="created")
    assert client.post(f"/orders/{created.id}/pay", headers=h).status_code == 403


def test_buyer_cannot_claim_or_close_ticket(client, engine):
    """买家不得领单/关单,更不得冒充坐席 id。"""
    with Session(engine) as s:
        tid = s.scalars(select(Ticket).where(Ticket.status == "queued")).first().id
    h = bearer(client, "buyer_demo")
    r = client.post(f"/tickets/{tid}/claim", headers=h, json={"service_id": "seed-agent-1"})
    assert r.status_code == 403, r.text
    assert client.post(f"/tickets/{tid}/close", headers=h).status_code == 403
    with Session(engine) as s:
        assert s.get(Ticket, tid).status == "queued", "工单状态不该被买家改动"


def test_buyer_cannot_approve_reject_or_fund_refund(client, engine):
    """核心红线:买家不得自审、自驳、自打款。"""
    order = _order(engine, status="delivered", customer="seed-cust-001")
    h = bearer(client, "buyer_demo")
    r = _create_refund(client, h, order)
    assert r.status_code == 201, r.text
    rid = r.json()["refund_request_id"]

    assert client.post(f"/refunds/{rid}/approve", headers=h,
                       json={"decided_by": "buyer_demo"}).status_code == 403
    assert client.post(f"/refunds/{rid}/reject", headers=h,
                       json={"decided_by": "buyer_demo"}).status_code == 403
    assert client.post(f"/refunds/{rid}/refund", headers=h).status_code == 403

    with Session(engine) as s:
        rf, od = s.get(RefundRequest, rid), s.get(Order, order.id)
        assert rf.status == "申请", "退款单必须停在申请态,等人工审核"
        assert od.status == "delivered", "订单不得被买家推成 refunded"


# ---------------------------------------------------------------------------
# 2) 买家只能动自己的单
# ---------------------------------------------------------------------------


def test_buyer_cannot_refund_another_customers_order(client, engine):
    """同店但不同买家:按"不存在"处理(404),不泄露他人订单存在性。"""
    other = _order(engine, status="delivered", customer="seed-cust-002")
    h = bearer(client, "buyer_demo")  # customer_id = seed-cust-001
    r = _create_refund(client, h, other)
    assert r.status_code == 404, r.text


def test_buyer_cannot_read_another_customers_refund(client, engine):
    """他人退款单详情同样按不存在处理。"""
    other = _order(engine, status="delivered", customer="seed-cust-002")
    ah = bearer(client, "agent_demo")
    r = _create_refund(client, ah, other)
    assert r.status_code == 201, r.text
    rid = r.json()["refund_request_id"]
    bh = bearer(client, "buyer_demo")
    assert client.get(f"/refunds/{rid}", headers=bh).status_code == 404


# ---------------------------------------------------------------------------
# 3) 审计字段从 token 取,请求体无身份效力
# ---------------------------------------------------------------------------


def test_decided_by_comes_from_token_not_body(client, engine):
    """坐席审核时 decided_by 必须是 token 主体,不能由请求体伪造。"""
    order = _order(engine, status="delivered", customer="seed-cust-001")
    bh = bearer(client, "buyer_demo")
    rid = _create_refund(client, bh, order).json()["refund_request_id"]
    ah = bearer(client, "agent_demo")
    r = client.post(f"/refunds/{rid}/approve", headers=ah,
                    json={"decided_by": "impersonated-root"})
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert s.get(RefundRequest, rid).decided_by == "seed-agent-1"


def test_claim_service_id_comes_from_token_not_body(client, engine):
    """领单坐席必须是 token 主体,不能冒充他人。"""
    with Session(engine) as s:
        tid = s.scalars(select(Ticket).where(Ticket.status == "queued")).first().id
    ah = bearer(client, "agent_demo")  # service_id = seed-agent-1
    r = client.post(f"/tickets/{tid}/claim", headers=ah, json={"service_id": "seed-agent-2"})
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert s.get(Ticket, tid).claimed_by == "seed-agent-1"


# ---------------------------------------------------------------------------
# 4) 正向:正常坐席链路不被守卫误伤
# ---------------------------------------------------------------------------


def test_agent_full_refund_flow_still_works(client, engine):
    """坐席建单→审核→打款完成仍应走通(防守卫过度)。"""
    order = _order(engine, status="delivered", customer="seed-cust-001")
    ah = bearer(client, "agent_demo")
    r = _create_refund(client, ah, order)
    assert r.status_code == 201, r.text
    rid = r.json()["refund_request_id"]
    assert client.post(f"/refunds/{rid}/approve", headers=ah, json={}).status_code == 200
    assert client.post(f"/refunds/{rid}/refund", headers=ah).status_code == 200
    with Session(engine) as s:
        assert s.get(RefundRequest, rid).status == "退款"
        assert s.get(Order, order.id).status == "refunded"


def test_buyer_can_still_create_refund_for_own_order(client, engine):
    """买家给自己订单建申请单是正当能力,不能被一并挡掉。"""
    order = _order(engine, status="delivered", customer="seed-cust-001")
    bh = bearer(client, "buyer_demo")
    r = _create_refund(client, bh, order)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "申请"

# ---------------------------------------------------------------------------
# B1(2026-11-17 审计):买家历史消息不得包含坐席内部备注
# ---------------------------------------------------------------------------

INTERNAL_NOTE = "内部备注:该买家疑似恶意退款,先拖延处理,勿告知客户"
PUBLIC_REPLY = "您好,已为您登记,正在核实。"


def _session_with_notes(engine) -> tuple[str, str]:
    """建一个属于 buyer_demo 的会话 + 工单,工单里一条公开回复 + 一条内部备注。"""
    from app.models import ChatSession, TicketMessage

    sid = "sess-b1-" + uuid.uuid4().hex[:8]
    tid = "ticket-b1-" + uuid.uuid4().hex[:8]
    with Session(engine) as s:
        s.add(ChatSession(id=sid, merchant_id=MERCHANT,
                          customer_id="seed-cust-001", status="handed_off"))
        s.add(Ticket(id=tid, merchant_id=MERCHANT, customer_id="seed-cust-001",
                     session_id=sid, status="claimed", claimed_by="seed-agent-1"))
        s.flush()
        s.add(TicketMessage(ticket_id=tid, author_type="agent",
                            content=PUBLIC_REPLY, is_internal=False))
        s.add(TicketMessage(ticket_id=tid, author_type="agent",
                            content=INTERNAL_NOTE, is_internal=True))
        s.commit()
    return sid, tid


def test_buyer_history_excludes_agent_internal_notes(client, engine):
    """买家历史接口**不得返回**内部备注(不是"返回但打标记")。

    缺陷:WS 推送(api/agent.py)与前端渲染(app.js)都过滤 is_internal,
    唯独 /api/c/sessions/{id}/messages 全量 append 只带标记 →
    任何持买家 token 的人直接调 API 就能读到坐席内部备注原文。
    前端不渲染 ≠ 数据没交出去。
    """
    sid, _ = _session_with_notes(engine)
    h = bearer(client, "buyer_demo")
    r = client.get(f"/api/c/sessions/{sid}/messages", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    contents = [str(item.get("content") or "") for item in items]
    assert INTERNAL_NOTE not in contents, "内部备注原文不得出现在买家响应里"
    assert not any(INTERNAL_NOTE in c for c in contents)
    assert not any(item.get("is_internal") is True for item in items)
    assert PUBLIC_REPLY in contents, "公开回复必须照常可见(防守卫过度)"


def test_agent_side_still_sees_internal_notes(client, engine):
    """正向:坐席端工单详情仍能看到内部备注(过滤只针对买家侧)。"""
    _, tid = _session_with_notes(engine)
    h = bearer(client, "agent_demo")
    r = client.get(f"/api/b/tickets/{tid}", headers=h)
    assert r.status_code == 200, r.text
    messages = r.json()["messages"]
    assert any(
        m.get("content") == INTERNAL_NOTE and m.get("is_internal") is True
        for m in messages
    ), "坐席端必须仍可见内部备注"
