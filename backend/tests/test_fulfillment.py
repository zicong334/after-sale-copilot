"""W11 售后履约纵深包:部分退款折算 / 物流回填 + 验货回调状态机 / SLA 超时升级 + B1-B8。

覆盖口径(任务书 W11 验收):
- W11-1:refund_item 服务端折算、残额/累计 qty 校验、整单旧路径不回归、幂等键含子项摘要;
- W11-2:申请→审核→退货中→待验货→退款/驳回全迁移、非法迁移拒、HMAC 签名错 401、
  重放 409、部分退不触发 order.refunded、全单退完才 refunded;
- W11-3:假时钟 scan_breaches 只标超时 queued、sla__alert 事件、领取清零、队列字段;
- B1 订单按买家过滤 / B2 工单详情退款单 / B3 买家会话消息 / B4 seed 转人工原因枚举 /
  B5 suggest prompt 动态化 / B6 favicon 兜底 / B7 手动流转 / B8 确认前不置 handed_off。

红线:不改 data/eval;不调真实外部 API(验货回调用本地 HMAC 公式)。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import sla
from app.api import get_db
from app.api.fulfillment import compute_signature
from app.db import utc_now
from app.errors import AmountExceedsPaidError, AppError, StateConflictError
from app.guardrails import HANDOFF_REASON_ENUM
from app.main import app
from app.models import (
    ChatMessage,
    ChatSession,
    Merchant,
    Order,
    OrderItem,
    RefundItem,
    RefundRequest,
    Ticket,
    TicketMessage,
)
from app.tools.events import ToolEvent
from app.security import Identity, current_identity
from app.services import refunds as refund_svc
from scripts.seed import seed

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()


def _seed(
    session: Session,
    *,
    oid: str = "o-f",
    customer_id: str = "c-f",
    order_status: str = "delivered",
) -> None:
    """一店一单四子项:2×3000 + 4000 + 3000 = 13000 分(可重复调用建多张单)。"""
    if session.get(Merchant, "m-f") is None:
        session.add(Merchant(id="m-f", name="履约测试店"))
    if session.get(Order, oid) is None:
        session.add(
            Order(
                id=oid,
                merchant_id="m-f",
                customer_id=customer_id,
                status=order_status,
                total_cents=13000,
                currency="CNY",
            )
        )
        session.add_all(
            [
                OrderItem(id=f"{oid}-i1", order_id=oid, qty=2, unit_price_cents=3000),
                OrderItem(id=f"{oid}-i2", order_id=oid, qty=1, unit_price_cents=4000),
                OrderItem(id=f"{oid}-i3", order_id=oid, qty=1, unit_price_cents=3000),
            ]
        )
    session.commit()


def _create(session: Session, *, key: str, oid: str = "o-f", **kwargs) -> RefundRequest:
    refund, _ = refund_svc.create_refund_request(
        session,
        "m-f",
        oid,
        reason=kwargs.pop("reason", "质量问题"),
        idempotency_key=key,
        **kwargs,
    )
    return refund


def _approved(session: Session, *, key: str, oid: str = "o-f", **kwargs) -> RefundRequest:
    refund = _create(session, key=key, oid=oid, **kwargs)
    return refund_svc.approve(session, refund.id, "agent-w11")


# ---------------------------------------------------------------------------
# W11-1 部分退款折算
# ---------------------------------------------------------------------------


def test_partial_refund_items_server_side_amount(session):
    _seed(session)
    refund = _create(session, key="k-partial", items=[{"order_item_id": "o-f-i1", "qty": 1}])

    assert refund.status == "申请"
    assert refund.amount_cents == 3000, "金额由服务端按子项单价折算"
    assert len(refund.items) == 1
    item = refund.items[0]
    assert (item.order_item_id, item.qty, item.unit_cents, item.line_cents) == (
        "o-f-i1",
        1,
        3000,
        3000,
    )
    assert refund_svc.residual_amount(session, "o-f") == 13000, "未退款前残额=实付"


def test_partial_refund_amount_cents_ignored(session):
    """传 items 时 amount_cents 被忽略(以子项折算为准)。"""
    _seed(session)
    refund = _create(
        session,
        key="k-ignore-amount",
        amount_cents=999999,
        items=[{"order_item_id": "o-f-i2", "qty": 1}],
    )
    assert refund.amount_cents == 4000


def test_item_qty_exceeds_purchase_rejected(session):
    _seed(session)
    with pytest.raises(AppError) as exc:
        _create(session, key="k-over-qty", items=[{"order_item_id": "o-f-i1", "qty": 3}])
    assert exc.value.status_code == 400
    assert exc.value.param == "items"


def test_cumulative_qty_across_refunds_rejected(session):
    """同一子项累计退 qty 不得超过购买 qty(含其他活跃退款单占用)。"""
    _seed(session)
    _approved(session, key="k-qty-1", items=[{"order_item_id": "o-f-i1", "qty": 2}])
    with pytest.raises(AppError) as exc:
        _create(session, key="k-qty-2", items=[{"order_item_id": "o-f-i1", "qty": 1}])
    assert exc.value.status_code == 400, "2 件已全部占用,不能再退第 3 件"
    assert exc.value.param == "items"


def test_item_not_belong_to_order_rejected(session):
    _seed(session)
    _seed(session, oid="o-g", customer_id="c-g")
    with pytest.raises(AppError) as exc:
        _create(session, key="k-foreign-item", items=[{"order_item_id": "o-g-i1", "qty": 1}])
    assert exc.value.status_code == 400


def test_amount_exceeds_residual_rejected(session):
    _seed(session)
    refund = _approved(session, key="k-full", amount_cents=13000)
    refund_svc.mark_refunded(session, refund.id)
    assert refund_svc.residual_amount(session, "o-f") == 0
    with pytest.raises(AmountExceedsPaidError):
        _create(session, key="k-over-residual", amount_cents=100)


def test_items_amount_exceeds_residual_rejected(session):
    _seed(session)
    first = _approved(session, key="k-first-8000", amount_cents=8000)
    refund_svc.mark_refunded(session, first.id)
    # 残额 5000;退 2 件(i1 一件 3000 + i2 一件 4000 = 7000)超残额
    with pytest.raises(AmountExceedsPaidError):
        _create(
            session,
            key="k-items-over",
            items=[
                {"order_item_id": "o-f-i1", "qty": 1},
                {"order_item_id": "o-f-i2", "qty": 1},
            ],
        )


def test_whole_order_legacy_path_still_refunds_order(session):
    """整单退款(不传 items)旧行为不变:审核→退款→订单 refunded。"""
    _seed(session)
    refund = _approved(session, key="k-legacy", amount_cents=13000)
    assert refund.status == "审核"
    refund_svc.mark_refunded(session, refund.id)
    assert refund.status == "退款"
    assert session.get(Order, "o-f").status == "refunded"


def test_partial_refund_does_not_refund_order(session):
    _seed(session)
    refund = _approved(session, key="k-part-1", items=[{"order_item_id": "o-f-i1", "qty": 1}])
    refund_svc.mark_refunded(session, refund.id)
    assert session.get(Order, "o-f").status == "delivered", "残额>0 不得把订单置 refunded"
    assert refund_svc.residual_amount(session, "o-f") == 10000

    second = _approved(session, key="k-part-2", amount_cents=10000)
    refund_svc.mark_refunded(session, second.id)
    assert session.get(Order, "o-f").status == "refunded", "全单退完才置 refunded"


def test_idempotency_key_includes_items_digest():
    from app.tools.refund import compute_refund_key

    key_a = compute_refund_key("o-f", "质量问题", None, [{"order_item_id": "o-f-i1", "qty": 1}])
    key_b = compute_refund_key("o-f", "质量问题", None, [{"order_item_id": "o-f-i3", "qty": 1}])
    assert key_a != key_b, "同金额不同子项的幂等键必须不同"
    assert key_a == compute_refund_key(
        "o-f", "质量问题", None, [{"order_item_id": "o-f-i1", "qty": 1}]
    )
    legacy = hashlib.sha256("o-f|refund_request|质量问题|3000".encode()).hexdigest()
    assert compute_refund_key("o-f", "质量问题", 3000) == legacy, "整单旧幂等键格式不变"


def test_refund_api_partial_items(client, engine):
    _seed(Session(engine))
    payload = {
        "merchant_id": "m-f",
        "order_id": "o-f",
        "amount_cents": 999999,
        "reason": "质量问题",
        "idempotency_key": "api-k-items",
        "items": [{"order_item_id": "o-f-i2", "qty": 1}],
    }
    resp = client.post("/refunds", json=payload)
    assert resp.status_code == 201, resp.text
    refund_id = resp.json()["refund_request_id"]
    assert resp.json()["idempotent_hit"] is False

    detail = client.get(f"/refunds/{refund_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["amount_cents"] == 4000, "amount_cents 被服务端折算覆盖"
    assert body["items"][0]["order_item_id"] == "o-f-i2"
    assert body["items"][0]["line_cents"] == 4000

    again = client.post("/refunds", json=payload)
    assert again.status_code == 201
    assert again.json()["refund_request_id"] == refund_id
    assert again.json()["idempotent_hit"] is True


def test_tool_refund_items_quote_amount(session):
    """refund__create 传 items 时由服务端折算(忽略 amount_cents)。"""
    from app.tools import ToolContext, invoke

    _seed(session)
    ctx = ToolContext(
        session=session, merchant_id="m-f", customer_id="c-f", trace_id="trace-w11-items"
    )
    checked = invoke(
        "policy__check_return",
        {"order_status": "delivered", "reason": "质量问题", "days_since_delivery": 3},
        ctx,
    )
    assert checked.ok is True

    res = invoke(
        "refund__create",
        {
            "order_id": "o-f",
            "reason": "质量问题",
            "items": [{"order_item_id": "o-f-i1", "qty": 1}],
        },
        ctx,
    )
    assert res.ok is True, res.error
    card = res.data["pending_confirm"]
    assert "3000" in card["what"]


def test_tool_refund_input_requires_amount_or_items():
    from app.tools.refund import Input

    with pytest.raises(ValueError):
        Input(order_id="o-f", reason="质量问题")


# ---------------------------------------------------------------------------
# W11-2 物流回填 + 验货回调状态机
# ---------------------------------------------------------------------------


def test_refund_state_machine_full(session):
    _seed(session)
    refund = _approved(session, key="k-machine", amount_cents=13000)

    refund = refund_svc.mark_shipping(session, refund.id, "SF1234567890")
    assert refund.status == "退货中"
    assert refund.tracking_no == "SF1234567890"

    refund = refund_svc.mark_received(session, refund.id)
    assert refund.status == "待验货"

    refund = refund_svc.inspect(session, refund.id, passed=True, decided_by="warehouse")
    assert refund.status == "退款"
    assert session.get(Order, "o-f").status == "refunded"


def test_illegal_transitions_rejected(session):
    _seed(session)
    refund = _create(session, key="k-illegal", amount_cents=3000)
    with pytest.raises(StateConflictError):
        refund_svc.mark_shipping(session, refund.id, "SF1234567890")

    refund_svc.approve(session, refund.id, "agent-w11")
    with pytest.raises(StateConflictError):
        refund_svc.mark_received(session, refund.id)

    _seed(session, oid="o-g", customer_id="c-g")
    other = _create(session, key="k-illegal-2", oid="o-g", amount_cents=3000)
    with pytest.raises(StateConflictError):
        refund_svc.inspect(session, other.id, passed=True)


def test_inspection_reject_updates_ticket_summary(session):
    _seed(session)
    session.add(
        Ticket(
            id="t-f",
            merchant_id="m-f",
            customer_id="c-f",
            order_id="o-f",
            status="queued",
            summary="买家申请退货",
        )
    )
    session.commit()

    refund = _approved(session, key="k-reject", amount_cents=3000)
    refund = refund_svc.inspect(
        session, refund.id, passed=False, reason="包装破损影响二次销售"
    )
    assert refund.status == "驳回"
    assert "包装破损" in session.get(Ticket, "t-f").summary
    assert session.get(Order, "o-f").status == "delivered"


def test_shipping_api_tracking_validation(client, engine):
    with Session(engine) as session:
        _seed(session)
        refund = _approved(session, key="k-ship-api", amount_cents=3000)
        refund_id = refund.id

    bad = client.post(f"/api/c/refunds/{refund_id}/shipping", json={"tracking_no": "abc"})
    assert bad.status_code == 400, "单号格式非法(全局 400 VALIDATION_ERROR)"
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

    ok = client.post(
        f"/api/c/refunds/{refund_id}/shipping", json={"tracking_no": "SF1234567890"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "退货中"

    duplicate = client.post(
        f"/api/c/refunds/{refund_id}/shipping", json={"tracking_no": "SF1234567890"}
    )
    assert duplicate.status_code == 409


def _signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return raw, {"X-Signature": compute_signature(raw), "Content-Type": "application/json"}


def test_inspection_api_signature_and_replay(client, engine):
    with Session(engine) as session:
        _seed(session)
        refund = _approved(session, key="k-callback", amount_cents=3000)
        refund_id = refund.id

    body = {"refund_id": refund_id, "passed": True, "reason": None, "nonce": "nonce-inspect-1"}
    raw, headers = _signed(body)

    bad_headers = dict(headers)
    bad_headers["X-Signature"] = "0" * 64
    bad = client.post("/api/fulfillment/inspection", content=raw, headers=bad_headers)
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "FULFILLMENT_SIGNATURE_INVALID"

    ok = client.post("/api/fulfillment/inspection", content=raw, headers=headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "退款"

    replay = client.post("/api/fulfillment/inspection", content=raw, headers=headers)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "REPLAY_DETECTED"


def test_receipt_callback_moves_to_waiting_inspection(client, engine):
    with Session(engine) as session:
        _seed(session)
        refund = _approved(session, key="k-receipt", amount_cents=3000)
        refund_svc.mark_shipping(session, refund.id, "SF1234567890")
        refund_id = refund.id

    body = {"refund_id": refund_id, "nonce": "nonce-receipt-1"}
    raw, headers = _signed(body)
    resp = client.post("/api/fulfillment/receipt", content=raw, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "待验货"


def test_partial_callback_does_not_refund_order(client, engine):
    """部分退走验货回调通过:退款单完成但订单仍 delivered(残额>0)。"""
    with Session(engine) as session:
        _seed(session)
        refund = _approved(session, key="k-partial-cb", amount_cents=3000)
        refund_id = refund.id

    body = {"refund_id": refund_id, "passed": True, "reason": None, "nonce": "nonce-partial-1"}
    raw, headers = _signed(body)
    assert client.post("/api/fulfillment/inspection", content=raw, headers=headers).status_code == 200
    with Session(engine) as session:
        assert session.get(Order, "o-f").status == "delivered"


# ---------------------------------------------------------------------------
# W11-3 SLA 超时升级
# ---------------------------------------------------------------------------


def _ticket(
    session: Session,
    tid: str,
    *,
    status: str = "queued",
    created_at: datetime,
    sla_breached: bool = False,
) -> None:
    session.add(
        Ticket(
            id=tid,
            merchant_id="m-f",
            customer_id="c-f",
            status=status,
            created_at=created_at,
            sla_breached=sla_breached,
            claimed_by="agent-a" if status in {"claimed", "waiting_customer"} else None,
        )
    )


def test_scan_breaches_marks_only_stale_queued(engine):
    now = utc_now()
    with Session(engine) as session:
        session.add(Merchant(id="m-f", name="履约测试店"))
        _ticket(session, "t-old", created_at=now - timedelta(minutes=31))
        _ticket(session, "t-new", created_at=now - timedelta(minutes=5))
        _ticket(session, "t-claimed", status="claimed", created_at=now - timedelta(minutes=90))
        session.commit()

        breached = sla.scan_breaches(session, now=now)
        assert [row.id for row in breached] == ["t-old"]
        assert session.get(Ticket, "t-old").sla_breached is True
        assert session.get(Ticket, "t-new").sla_breached is False
        assert session.get(Ticket, "t-claimed").sla_breached is False, "已领取不升级"

        events = list(
            session.scalars(
                select(ToolEvent).where(ToolEvent.tool_name == sla.EVENT_SLA_ALERT)
            )
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload)
        assert payload["ticket_id"] == "t-old"
        assert payload["threshold_minutes"] == sla.queued_threshold_minutes()

        assert sla.scan_breaches(session, now=now) == [], "重复扫描不得重复告警"


def test_claim_clears_sla_flag(client, engine):
    now = utc_now()
    with Session(engine) as session:
        session.add(Merchant(id="m-f", name="履约测试店"))
        _ticket(session, "t-clear", created_at=now - timedelta(minutes=60), sla_breached=True)
        session.commit()

    resp = client.post("/tickets/t-clear/claim", json={"service_id": "agent-w11"})
    assert resp.status_code == 200, resp.text
    with Session(engine) as session:
        ticket = session.get(Ticket, "t-clear")
        assert ticket.status == "claimed"
        assert ticket.sla_breached is False, "领取后清零"


def test_ticket_queue_exposes_sla_fields(client, engine):
    now = utc_now()
    with Session(engine) as session:
        session.add(Merchant(id="m-f", name="履约测试店"))
        _ticket(session, "t-queue", created_at=now - timedelta(minutes=60), sla_breached=True)
        session.commit()

    resp = client.get("/api/b/tickets")
    assert resp.status_code == 200, resp.text
    item = next(row for row in resp.json()["items"] if row["ticket_id"] == "t-queue")
    assert item["sla_breached"] is True
    assert item["sla_queued_minutes"] == sla.queued_threshold_minutes()


# ---------------------------------------------------------------------------
# B1-B8 回补
# ---------------------------------------------------------------------------


def test_b1_orders_filtered_by_buyer_customer(client, engine):
    with Session(engine) as session:
        _seed(session)
        _seed(session, oid="o-other", customer_id="c-other")

    app.dependency_overrides[current_identity] = lambda: Identity(
        sub="buyer-b1", role="buyer", merchant_id="m-f", customer_id="c-f"
    )
    try:
        resp = client.get("/orders")
    finally:
        app.dependency_overrides.pop(current_identity, None)
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()]
    assert "o-f" in ids
    assert "o-other" not in ids, "B1:不得读到同店其他买家的订单"


def test_b2_ticket_detail_includes_refund(client, engine):
    with Session(engine) as session:
        _seed(session)
        session.add(
            Ticket(
                id="t-b2",
                merchant_id="m-f",
                customer_id="c-f",
                order_id="o-f",
                status="queued",
            )
        )
        refund = _create(session, key="k-b2", amount_cents=3000)
        refund_id = refund.id
        session.commit()

    resp = client.get("/tickets/t-b2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["refund_request_id"] == refund_id
    assert body["refund_status"] == "申请"


def test_b3_buyer_session_messages_include_agent_reply(client, engine):
    with Session(engine) as session:
        _seed(session)
        session.add(
            ChatSession(
                id="s-b3", merchant_id="m-f", customer_id="c-f", status="active"
            )
        )
        session.commit()
        session.add(ChatMessage(session_id="s-b3", role="user", content="我要退货"))
        session.add(
            Ticket(
                id="t-b3",
                merchant_id="m-f",
                customer_id="c-f",
                session_id="s-b3",
                status="queued",
            )
        )
        session.commit()
        session.add(
            TicketMessage(
                ticket_id="t-b3", author_type="agent", content="已收到,请提供物流单号"
            )
        )
        session.commit()

    resp = client.get("/api/c/sessions/s-b3/messages")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert {row["source"] for row in body["items"]} == {"chat", "ticket"}
    assert any("物流单号" in row["content"] for row in body["items"])

    missing = client.get("/api/c/sessions/s-missing/messages")
    assert missing.status_code == 404


def test_b4_seed_handoff_reasons_align_enum(engine):
    with Session(engine) as session:
        seed(session)
        reasons = {
            row
            for row in session.scalars(select(Ticket.handoff_reason))
            if row is not None
        }
    assert reasons, "seed 必须带转人工原因"
    assert reasons <= set(HANDOFF_REASON_ENUM), reasons - set(HANDOFF_REASON_ENUM)


def test_b5_suggest_prompt_dynamic(client, engine):
    from types import SimpleNamespace

    from app.api.agent import _suggest_prompt

    ticket = SimpleNamespace(
        id="t-b5",
        status="claimed",
        order_id="o-f",
        handoff_reason="quality",
        summary="买家称衣服有破洞",
    )
    messages = [
        SimpleNamespace(author_type="user", content="衣服破了,我要退货"),
        SimpleNamespace(author_type="agent", content="已为您登记,请稍等"),
    ]
    citations = [
        SimpleNamespace(clause_id="RP-12", title="质量问题退货", text="7 天内可退换")
    ]
    prompt = _suggest_prompt(ticket, messages, citations)
    assert "衣服破了" in prompt, "转录必须进入 prompt"
    assert "RP-12" in prompt and "7 天内可退换" in prompt, "命中条款必须进入 prompt"
    assert "不得承诺" in prompt, "硬约束必须保留"


def test_b6_favicon_fallback(client):
    for path in ("/favicon.ico", "/favicon.svg"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "svg" in resp.headers.get("content-type", "")
        assert resp.text.lstrip().startswith("<svg")


def test_b7_manual_ticket_transition_flow(client, engine):
    now = utc_now()
    with Session(engine) as session:
        session.add(Merchant(id="m-f", name="履约测试店"))
        _ticket(session, "t-b7", created_at=now)
        session.commit()

    for to_status, expected in (
        ("claimed", "claimed"),
        ("waiting_customer", "waiting_customer"),
        ("closed", "closed"),
    ):
        resp = client.post(
            "/tickets/t-b7/status",
            json={"to_status": to_status, "service_id": "agent-w11"},
        )
        assert resp.status_code == 200, (to_status, resp.text)
        assert resp.json()["status"] == expected

    backward = client.post(
        "/tickets/t-b7/status", json={"to_status": "claimed", "service_id": "agent-w11"}
    )
    assert backward.status_code == 409, "B7:回退流转必须拒绝"


def test_b8_frontend_pending_handoff_badge_static():
    app_js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "pendingHandoff" in app_js
    assert "待确认转人工" in app_js
    assert 'handoff__create_ticket' in app_js
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    assert 'data-state="pending-handoff"' in css


def test_w11_3_frontend_sla_badge_static():
    agent_js = (FRONTEND / "agent.js").read_text(encoding="utf-8")
    assert "slaBadge" in agent_js
    assert "sla_breached" in agent_js
    assert "SLA 超时" in agent_js
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    assert ".badge-sla" in css


# ---------------------------------------------------------------------------
# A2(2026-11-16 审计):非 delivered 订单退全款的状态机死角
# ---------------------------------------------------------------------------


def test_rest_rejects_refund_on_non_refundable_order(client, engine):
    """非 ``delivered`` 订单不得建退款单(从源头拒掉,不留脏单)。

    缺陷:建单侧(服务层 + REST)不看 ``order.status``,而完成侧 ``refund_complete``
    守卫要求 ``delivered``。于是 ``shipped`` 订单退全款时,最后一笔把残额退到 0
    会 409 并 ``rollback``——**连退款单自身置「退款」的更新都被回滚**,
    这笔退款永久卡在「审核」态。而 ``物流破损`` 是 schema 明确允许的退款原因,
    天然发生在运输途中,属现实场景。

    修法:REST 入口与工具层同口径(``tools/refund.REFUNDABLE_STATUSES``)。
    门放在**入口**而不是服务层:服务层是机制(金额/幂等),入口才是政策落点,
    这样只测金额数学的服务层单测不受影响。
    """
    with Session(engine) as session:
        _seed(session, oid="o-shipped", order_status="shipped")
    r = client.post(
        "/refunds",
        json={
            "merchant_id": "m-f",
            "order_id": "o-shipped",
            "amount_cents": 13000,
            "reason": "物流破损",
            "idempotency_key": "a2-shipped",
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "STATE_CONFLICT"
    with Session(engine) as session:
        assert (
            session.scalars(
                select(RefundRequest).where(RefundRequest.order_id == "o-shipped")
            ).first()
            is None
        ), "被拒的请求不得留下退款单"


def test_rest_still_allows_refund_on_delivered_order(client, engine):
    """正向:delivered 订单建单不受新门影响(防守卫过度)。"""
    with Session(engine) as session:
        _seed(session, oid="o-ok", order_status="delivered")
    r = client.post(
        "/refunds",
        json={
            "merchant_id": "m-f",
            "order_id": "o-ok",
            "amount_cents": 13000,
            "reason": "质量问题",
            "idempotency_key": "a2-delivered",
        },
    )
    assert r.status_code == 201, r.text
