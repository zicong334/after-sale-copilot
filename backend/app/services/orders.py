"""订单状态推进服务:全部守卫式更新(03§4.1.3 迁移表)。

迁移表:created → paid → shipped → delivered;paid/shipped/delivered → refunded。
守卫式:``UPDATE ... WHERE id=? AND status='旧'``,影响行数 != 1 → 409 STATE_CONFLICT
(带当前真实状态);禁止 SELECT 后 UPDATE(TOCTOU)。
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import NotFoundError, StateConflictError
from app.models import Order, RefundRequest


def _advance(
    session: Session, order_id: str, from_status: str, to_status: str
) -> Order:
    """通用守卫式状态推进(单步迁移)。"""
    result = session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == from_status)
        .values(status=to_status)
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(Order, order_id)
        if current is None:
            raise NotFoundError(f"订单不存在: {order_id}")
        raise StateConflictError(
            f"订单状态迁移失败: 期望 {from_status} → {to_status}", current.status
        )
    session.commit()
    return session.get(Order, order_id)


def pay(session: Session, order_id: str) -> Order:
    """支付:created → paid。"""
    return _advance(session, order_id, "created", "paid")


def ship(session: Session, order_id: str) -> Order:
    """发货:paid → shipped。"""
    return _advance(session, order_id, "paid", "shipped")


def deliver(session: Session, order_id: str) -> Order:
    """签收:shipped → delivered。"""
    return _advance(session, order_id, "shipped", "delivered")


def refund_complete(session: Session, order_id: str) -> Order:
    """退款完成:delivered → refunded。

    守卫条件(03 迁移表 + G2 卡):存在该订单 ``status='退款'`` 的退款单。
    """
    exists_refunded = (
        select(RefundRequest.id)
        .where(
            RefundRequest.order_id == order_id,
            RefundRequest.status == "退款",
        )
        .exists()
    )
    result = session.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.status == "delivered",
            exists_refunded,
        )
        .values(status="refunded")
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(Order, order_id)
        if current is None:
            raise NotFoundError(f"订单不存在: {order_id}")
        raise StateConflictError(
            "订单退款完成失败: 需 delivered 且存在 status='退款' 的退款单",
            current.status,
        )
    session.commit()
    return session.get(Order, order_id)
