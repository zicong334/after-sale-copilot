"""订单状态推进服务:全部守卫式更新(03§4.1.3 迁移表)。

迁移表:created → paid → shipped → delivered;paid/shipped/delivered → refunded。
守卫式:``UPDATE ... WHERE id=? AND status='旧'``,影响行数 != 1 → 409 STATE_CONFLICT
(带当前真实状态);禁止 SELECT 后 UPDATE(TOCTOU)。
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import utc_now
from app.errors import NotFoundError, StateConflictError
from app.models import Order, RefundRequest


def _advance(
    session: Session, order_id: str, from_status: str, to_status: str,
    *, extra_values: dict | None = None,
) -> Order:
    """通用守卫式状态推进(单步迁移)。

    ``extra_values`` 用于随流转一并写入的时间戳(如签收时间,F1),
    与状态更新在**同一条 UPDATE** 里,不会出现"状态改了时间没写"的中间态。
    """
    values = {"status": to_status, **(extra_values or {})}
    result = session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == from_status)
        .values(**values)
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
    """签收:shipped → delivered,并写入签收时间(F1)。

    签收时间是政策窗口(七天无理由 / 15 天质量问题)的**唯一正确锚点**,
    必须在这里落库,否则下游只能拿下单时间兜底而系统性误拒。
    """
    return _advance(
        session, order_id, "shipped", "delivered",
        extra_values={"delivered_at": utc_now()},
    )


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
