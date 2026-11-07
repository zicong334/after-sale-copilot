"""退款服务:create(幂等)/approve/reject/mark_refunded,每步产出 Receipt。

迁移表(03§4.1.3 + G2 卡):
- 申请 → 审核(approve,记 decided_by/decided_at)
- 申请 → 驳回(reject)
- 审核 → 退款(mark_refunded;同时守卫式把 order 推 delivered → refunded,
  守卫条件含"存在 status='退款' 的退款单")

幂等(PRD §5):create_refund_request 先按 idempotency_key 查,命中返回已有单
(idempotent_hit=true);金额 > 订单实付 → 422 AMOUNT_EXCEEDS_PAID 不建单;
uq_refund_active 冲突 → 409。
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import utc_now
from app.errors import AmountExceedsPaidError, NotFoundError, StateConflictError
from app.models import Order, RefundRequest
from app.services.orders import refund_complete


def _get_refund(session: Session, refund_id: str) -> RefundRequest:
    refund = session.get(RefundRequest, refund_id)
    if refund is None:
        raise NotFoundError(f"退款单不存在: {refund_id}")
    return refund


def create_refund_request(
    session: Session,
    merchant_id: str,
    order_id: str,
    amount_cents: int,
    reason: str,
    idempotency_key: str,
) -> tuple[RefundRequest, bool]:
    """创建退款申请单。返回 (退款单, idempotent_hit)。

    幂等命中:按 idempotency_key 查,命中返回已有单而非报错。
    """
    # 1. 幂等命中
    existing = session.execute(
        select(RefundRequest).where(RefundRequest.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True

    # 2. 金额校验:金额 > 订单实付 → 422,不建单
    order = session.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    if amount_cents > order.total_cents:
        raise AmountExceedsPaidError(
            f"退款金额 {amount_cents} 分超过订单实付 {order.total_cents} 分"
        )

    # 3. 建单(status 默认 '申请')
    refund = RefundRequest(
        merchant_id=merchant_id,
        order_id=order_id,
        amount_cents=amount_cents,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    session.add(refund)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # 并发下撞唯一键:幂等键命中则返回已有单;否则是 uq_refund_active 冲突
        existing = session.execute(
            select(RefundRequest).where(
                RefundRequest.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True
        raise StateConflictError(
            "该订单已存在活跃退款单(申请/审核/退款),不能重复创建",
            param="uq_refund_active",
        )
    session.refresh(refund)
    return refund, False


def _decide(
    session: Session,
    refund_id: str,
    from_status: str,
    to_status: str,
    decided_by: str,
) -> RefundRequest:
    """审核/驳回共用守卫式更新(申请 → 审核/驳回)。"""
    result = session.execute(
        update(RefundRequest)
        .where(RefundRequest.id == refund_id, RefundRequest.status == from_status)
        .values(status=to_status, decided_by=decided_by, decided_at=utc_now())
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(RefundRequest, refund_id)
        if current is None:
            raise NotFoundError(f"退款单不存在: {refund_id}")
        raise StateConflictError(
            f"退款单状态迁移失败: 期望 {from_status}", current.status
        )
    session.commit()
    return session.get(RefundRequest, refund_id)


def approve(session: Session, refund_id: str, decided_by: str) -> RefundRequest:
    """审核通过:申请 → 审核。"""
    return _decide(session, refund_id, "申请", "审核", decided_by)


def reject(session: Session, refund_id: str, decided_by: str) -> RefundRequest:
    """驳回:申请 → 驳回。"""
    return _decide(session, refund_id, "申请", "驳回", decided_by)


def mark_refunded(session: Session, refund_id: str) -> RefundRequest:
    """退款完成:审核 → 退款,并把订单 delivered → refunded(守卫式)。

    守卫1:退款单必须处于审核态;守卫2(在 refund_complete 内):订单必须 delivered
    且存在 status='退款' 的退款单。两步同一事务原子提交。
    """
    refund = _get_refund(session, refund_id)
    order_id = refund.order_id

    result = session.execute(
        update(RefundRequest)
        .where(RefundRequest.id == refund_id, RefundRequest.status == "审核")
        .values(status="退款")
    )
    if result.rowcount != 1:
        session.rollback()
        raise StateConflictError("退款单需处于审核态才能退款", refund.status)

    # 守卫式推进订单(内部 commit,原子提交 refund+order 两步);失败会回滚退款单
    refund_complete(session, order_id)
    return session.get(RefundRequest, refund_id)
