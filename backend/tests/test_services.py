"""W1-B 服务层测试:守卫式状态推进、并发 claim、关单后加消息拒绝、退款幂等、seed 幂等。

覆盖任务书测试要求:非法迁移 409、并发 claim 只一个成功、关单后 add_message 拒绝、
幂等键重复只一张单、金额超实付拦截、seed 跑两遍数据量不变。
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AmountExceedsPaidError, NotFoundError, StateConflictError
from app.models import Merchant, Order, RefundRequest, Ticket
from app.services import orders as order_svc
from app.services import refunds as refund_svc
from app.services import tickets as ticket_svc
from scripts.seed import seed


# ---------------------------------------------------------------------------
# 构造 helper
# ---------------------------------------------------------------------------

def _merchant(session: Session, mid: str = "m-1") -> Merchant:
    m = Merchant(id=mid, name="测试店")
    session.add(m)
    session.commit()
    return m


def _order(
    session: Session,
    mid: str = "m-1",
    oid: str = "o-1",
    status: str = "created",
    total: int = 10000,
) -> Order:
    o = Order(
        id=oid,
        merchant_id=mid,
        customer_id="cust-1",
        status=status,
        total_cents=total,
        currency="CNY",
    )
    session.add(o)
    session.commit()
    return o


# ---------------------------------------------------------------------------
# 订单守卫式推进
# ---------------------------------------------------------------------------

def test_pay_created_to_paid(session):
    _merchant(session)
    _order(session, oid="o-1", status="created")
    order = order_svc.pay(session, "o-1")
    assert order.status == "paid"


def test_pay_illegal_transition_conflict(session):
    _merchant(session)
    _order(session, oid="o-1", status="paid")
    with pytest.raises(StateConflictError) as ei:
        order_svc.pay(session, "o-1")
    assert ei.value.param == "paid"


def test_ship_paid_to_shipped(session):
    _merchant(session)
    _order(session, oid="o-1", status="paid")
    assert order_svc.ship(session, "o-1").status == "shipped"


def test_deliver_shipped_to_delivered(session):
    _merchant(session)
    _order(session, oid="o-1", status="shipped")
    assert order_svc.deliver(session, "o-1").status == "delivered"


def test_order_not_found(session):
    with pytest.raises(NotFoundError):
        order_svc.pay(session, "no-such-order")


# ---------------------------------------------------------------------------
# 工单守卫式推进
# ---------------------------------------------------------------------------

def test_ticket_claim_queued_to_claimed(session):
    _merchant(session)
    session.add(Ticket(id="tk-1", merchant_id="m-1", customer_id="cust-1", status="queued"))
    session.commit()
    ticket = ticket_svc.claim(session, "tk-1", "agent-1")
    assert ticket.status == "claimed"
    assert ticket.claimed_by == "agent-1"


def test_ticket_concurrent_claim_only_one_succeeds(engine):
    """两个 session 模拟并发 claim 同一工单,只有一个成功。"""
    with Session(engine) as s:
        s.add(Merchant(id="m-1", name="测试店"))
        s.add(Ticket(id="tk-1", merchant_id="m-1", customer_id="cust-1", status="queued"))
        s.commit()

    s1 = Session(engine)
    s2 = Session(engine)
    assert ticket_svc.claim(s1, "tk-1", "agent-1").status == "claimed"
    with pytest.raises(StateConflictError) as ei:
        ticket_svc.claim(s2, "tk-1", "agent-2")
    assert ei.value.param == "claimed"
    s1.close()
    s2.close()


def test_ticket_close(session):
    _merchant(session)
    session.add(Ticket(id="tk-1", merchant_id="m-1", customer_id="cust-1", status="claimed", claimed_by="agent-1"))
    session.commit()
    assert ticket_svc.close(session, "tk-1").status == "closed"


def test_ticket_add_message(session):
    _merchant(session)
    session.add(Ticket(id="tk-1", merchant_id="m-1", customer_id="cust-1", status="queued"))
    session.commit()
    msg = ticket_svc.add_message(session, "tk-1", "user", "你好", False)
    assert msg.author_type == "user"
    assert session.get(Ticket, "tk-1").last_msg_at is not None


def test_ticket_add_message_after_close_rejected(session):
    _merchant(session)
    session.add(Ticket(id="tk-1", merchant_id="m-1", customer_id="cust-1", status="closed"))
    session.commit()
    with pytest.raises(StateConflictError) as ei:
        ticket_svc.add_message(session, "tk-1", "user", "补充", False)
    assert ei.value.param == "closed"


# ---------------------------------------------------------------------------
# 退款:幂等 / 金额校验 / 推进
# ---------------------------------------------------------------------------

def test_refund_create(session):
    _merchant(session)
    _order(session, oid="o-1", status="delivered", total=10000)
    refund, hit = refund_svc.create_refund_request(
        session, "m-1", "o-1", 5000, "质量问题", "ik-1"
    )
    assert hit is False
    assert refund.status == "申请"


def test_refund_create_idempotent_key_hit(session):
    _merchant(session)
    _order(session, oid="o-1", status="delivered", total=10000)
    r1, hit1 = refund_svc.create_refund_request(session, "m-1", "o-1", 5000, "质量问题", "ik-1")
    assert hit1 is False
    r2, hit2 = refund_svc.create_refund_request(session, "m-1", "o-1", 5000, "质量问题", "ik-1")
    assert hit2 is True
    assert r2.id == r1.id
    count = session.scalar(select(func.count()).select_from(RefundRequest))
    assert count == 1


def test_refund_create_amount_exceeds_paid(session):
    _merchant(session)
    _order(session, oid="o-1", status="paid", total=10000)
    with pytest.raises(AmountExceedsPaidError):
        refund_svc.create_refund_request(session, "m-1", "o-1", 10001, "质量问题", "ik-1")
    assert session.scalar(select(func.count()).select_from(RefundRequest)) == 0


def test_refund_approve_then_reject_conflict(session):
    _merchant(session)
    _order(session, oid="o-1", status="delivered", total=10000)
    refund, _ = refund_svc.create_refund_request(session, "m-1", "o-1", 5000, "质量问题", "ik-1")
    assert refund_svc.approve(session, refund.id, "agent-1").status == "审核"
    with pytest.raises(StateConflictError):
        refund_svc.reject(session, refund.id, "agent-1")


def test_refund_mark_refunded_full_chain(session):
    _merchant(session)
    _order(session, oid="o-1", status="delivered", total=10000)
    refund, _ = refund_svc.create_refund_request(session, "m-1", "o-1", 10000, "质量问题", "ik-1")
    assert refund_svc.approve(session, refund.id, "agent-1").status == "审核"
    assert refund_svc.mark_refunded(session, refund.id).status == "退款"
    assert session.get(Order, "o-1").status == "refunded"


# ---------------------------------------------------------------------------
# seed 幂等
# ---------------------------------------------------------------------------

def test_seed_idempotent(engine):
    with Session(engine) as s:
        first = seed(s)
        second = seed(s)
    assert first == second
    assert first["order"] == 30
    assert first["merchant"] == 1
    assert first["ticket"] == 8
