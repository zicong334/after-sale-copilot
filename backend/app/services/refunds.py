"""退款服务:create(幂等 + 子项折算)/approve/reject/shipping/receipt/inspection。

迁移表(W11 任务书 + 03§4.1.3):

==========  ==========  ==========================================
申请        审核        商家审核通过(approve,记 decided_by/decided_at)
申请        驳回        商家审核驳回(reject)
审核        退货中      买家回填物流单号(mark_shipping)
退货中      待验货      仓库收货确认(mark_received)
审核        退款/驳回   验货通过/不通过(inspection)
待验货      退款/驳回   验货通过/不通过(inspection)
==========  ==========  ==========================================

W11-1 部分退款折算
------------------
- 传 ``items`` 时由服务端按 ``order_item.unit_price_cents`` 折算,退款总额 = Σ line_cents;
  调用方给的 amount_cents 被忽略。
- 金额上限改为"实付残额" = order.total_cents - Σ(已退款单金额);
- 同一 order_item 的历史累计退 qty(含在途单)不得超过购买 qty。

W11-2 订单状态联动
------------------
只有退款单累计金额覆盖整单实付(残额 <= 0)时才把 order 推 ``delivered → refunded``;
部分退款完成后订单仍是 ``delivered``,可继续发起第二笔部分退款。

幂等(PRD §5):create_refund_request 先按 idempotency_key 查,命中返回已有单
(idempotent_hit=true);金额 > 实付残额 → 422 AMOUNT_EXCEEDS_PAID 不建单;
uq_refund_active 冲突 → 409。
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import utc_now
from app.errors import (
    AmountExceedsPaidError,
    AppError,
    NotFoundError,
    StateConflictError,
)
from app.models import Order, OrderItem, RefundItem, RefundRequest, Ticket
from app.services.orders import refund_complete

#: 活跃退款单状态(与迁移 0009 的 uq_refund_active 部分唯一索引口径一致)。
ACTIVE_REFUND_STATUSES: tuple[str, ...] = ("申请", "审核", "退货中", "待验货")
#: 已完成退款的终态。
COMPLETED_STATUS = "退款"
#: 被驳回的终态。
REJECTED_STATUS = "驳回"
#: 可以继续做"验货终审"的状态。
FUNDABLE_STATUSES: tuple[str, ...] = ("审核", "待验货")
#: 计入"已占用/已退"数量的退款单状态(驳回的不占用 qty 与金额)。
_COUNTED_STATUSES: tuple[str, ...] = ACTIVE_REFUND_STATUSES + (COMPLETED_STATUS,)


def _get_refund(session: Session, refund_id: str) -> RefundRequest:
    refund = session.get(RefundRequest, refund_id)
    if refund is None:
        raise NotFoundError(f"退款单不存在: {refund_id}")
    return refund


def refunded_amount(session: Session, order_id: str) -> int:
    """该订单已退款总额(只统计 status='退款' 的终态退款单)。"""
    total = session.execute(
        select(func.coalesce(func.sum(RefundRequest.amount_cents), 0)).where(
            RefundRequest.order_id == order_id,
            RefundRequest.status == COMPLETED_STATUS,
        )
    ).scalar_one()
    return int(total)


def residual_amount(session: Session, order_id: str) -> int:
    """实付残额 = 订单实付 - 已退款总额(最小 0)。"""
    order = session.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    return max(int(order.total_cents) - refunded_amount(session, order_id), 0)


def _reserved_qty(session: Session, order_id: str) -> dict[str, int]:
    """订单各子项已被退款单占用的数量(含在途与已完成,驳回不计)。"""
    rows = session.execute(
        select(RefundItem.order_item_id, func.sum(RefundItem.qty))
        .join(RefundRequest, RefundItem.refund_request_id == RefundRequest.id)
        .where(
            RefundRequest.order_id == order_id,
            RefundRequest.status.in_(_COUNTED_STATUSES),
        )
        .group_by(RefundItem.order_item_id)
    ).all()
    return {str(item_id): int(qty or 0) for item_id, qty in rows}


def _pair(raw: Any) -> tuple[str, int]:
    """兼容 dict / pydantic 两种子项入参。"""
    if isinstance(raw, dict):
        return str(raw["order_item_id"]), int(raw["qty"])
    return str(getattr(raw, "order_item_id")), int(getattr(raw, "qty"))


def compute_refund_items(
    session: Session, order_id: str, items: Iterable[Any]
) -> tuple[int, list[dict[str, int | str]]]:
    """服务端折算子项退款额。返回 (amount_cents, 规范化子项列表)。

    校验:子项属于该订单、同单不重复、qty > 0、累计退 qty 不超过购买 qty;
    金额超过实付残额抛 ``AmountExceedsPaidError``。
    """
    pairs = [_pair(raw) for raw in items]
    if not pairs:
        raise AppError("VALIDATION_ERROR", "退款子项不能为空", status_code=400, param="items")

    order = session.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")

    order_items = {
        item.id: item
        for item in session.scalars(
            select(OrderItem).where(OrderItem.order_id == order_id)
        ).all()
    }
    reserved = _reserved_qty(session, order_id)

    seen: set[str] = set()
    normalized: list[dict[str, int | str]] = []
    amount = 0
    for order_item_id, qty in pairs:
        if order_item_id in seen:
            raise AppError(
                "VALIDATION_ERROR",
                f"同一子项不能重复提交: {order_item_id}",
                status_code=400,
                param="items",
            )
        seen.add(order_item_id)

        item = order_items.get(order_item_id)
        if item is None:
            raise AppError(
                "VALIDATION_ERROR",
                f"订单子项不存在或不属于该订单: {order_item_id}",
                status_code=400,
                param="items",
            )
        if qty <= 0:
            raise AppError(
                "VALIDATION_ERROR", "退款数量必须大于 0", status_code=400, param="items"
            )

        already = int(reserved.get(order_item_id, 0))
        available = int(item.qty) - already
        if qty > available:
            raise AppError(
                "VALIDATION_ERROR",
                f"子项 {order_item_id} 可退数量不足: 购买 {item.qty},"
                f"已退/在途 {already},本次 {qty}",
                status_code=400,
                param="items",
            )

        unit_cents = int(item.unit_price_cents)
        line_cents = qty * unit_cents
        amount += line_cents
        normalized.append(
            {
                "order_item_id": order_item_id,
                "qty": qty,
                "unit_cents": unit_cents,
                "line_cents": line_cents,
            }
        )

    if amount <= 0:
        raise AppError(
            "VALIDATION_ERROR", "退款金额必须大于 0", status_code=400, param="items"
        )
    residual = max(int(order.total_cents) - refunded_amount(session, order_id), 0)
    if amount > residual:
        raise AmountExceedsPaidError(
            f"退款金额 {amount} 分超过订单实付残额 {residual} 分"
        )
    return amount, normalized


def create_refund_request(
    session: Session,
    merchant_id: str,
    order_id: str,
    amount_cents: int | None = None,
    reason: str = "",
    idempotency_key: str = "",
    *,
    items: Iterable[Any] | None = None,
) -> tuple[RefundRequest, bool]:
    """创建退款申请单。返回 (退款单, idempotent_hit)。

    - ``items`` 非空:服务端折算金额,``amount_cents`` 被忽略(W11-1);
    - ``items`` 为空:沿用旧行为,金额由调用方给出(整单退款);
    - 金额上限为实付残额而非订单总额。
    """
    # 1. 幂等命中
    existing = session.execute(
        select(RefundRequest).where(RefundRequest.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True

    # 2. 订单存在性 + 金额折算/校验
    order = session.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")

    normalized: list[dict[str, int | str]] | None = None
    if items is not None:
        amount, normalized = compute_refund_items(session, order_id, items)
    elif amount_cents is None:
        raise AppError(
            "VALIDATION_ERROR",
            "必须提供 amount_cents 或 items",
            status_code=400,
            param="amount_cents",
        )
    else:
        amount = int(amount_cents)
        if amount <= 0:
            raise AppError(
                "VALIDATION_ERROR",
                "退款金额必须大于 0",
                status_code=400,
                param="amount_cents",
            )
        residual = residual_amount(session, order_id)
        if amount > residual:
            raise AmountExceedsPaidError(
                f"退款金额 {amount} 分超过订单实付残额 {residual} 分"
            )

    # 3. 建单(status 默认 '申请')+ 子项
    refund = RefundRequest(
        merchant_id=merchant_id,
        order_id=order_id,
        amount_cents=amount,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    session.add(refund)
    try:
        session.flush()  # 先拿 refund.id 再插子项
        if normalized:
            session.add_all(
                [
                    RefundItem(refund_request_id=refund.id, **row)
                    for row in normalized
                ]
            )
        session.commit()
    except IntegrityError:
        session.rollback()
        # 并发下撞唯一键:幂等键命中则返回已有单;否则是活跃单/qty 唯一约束冲突
        existing = session.execute(
            select(RefundRequest).where(
                RefundRequest.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True
        raise StateConflictError(
            "该订单已存在活跃退款单,不能重复创建", param="uq_refund_active"
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
    """审核驳回:申请 → 驳回。"""
    return _decide(session, refund_id, "申请", "驳回", decided_by)


def _append_ticket_summary(
    session: Session, order_id: str, text: str
) -> None:
    """把验货驳回结论追加到该订单最新工单摘要(W11-2 工单摘要更新)。"""
    ticket = session.execute(
        select(Ticket)
        .where(Ticket.order_id == order_id)
        .order_by(Ticket.created_at.desc())
        .limit(1)
    ).scalars().first()
    if ticket is None:
        return
    prefix = (ticket.summary or "").strip()
    merged = f"{prefix}\n{text}".strip() if prefix else text.strip()
    ticket.summary = merged[:1000]


def _apply_final(
    session: Session,
    refund_id: str,
    to_status: str,
    decided_by: str | None = None,
    *,
    summary_text: str | None = None,
) -> RefundRequest:
    """终审守卫式迁移:审核/待验货 → 退款/驳回,并做订单/工单联动。"""
    refund = _get_refund(session, refund_id)
    values: dict[str, Any] = {"status": to_status}
    if decided_by is not None:
        values["decided_by"] = decided_by
        values["decided_at"] = utc_now()
    result = session.execute(
        update(RefundRequest)
        .where(
            RefundRequest.id == refund_id,
            RefundRequest.status.in_(FUNDABLE_STATUSES),
        )
        .values(**values)
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(RefundRequest, refund_id)
        if current is None:
            raise NotFoundError(f"退款单不存在: {refund_id}")
        raise StateConflictError(
            "退款单需处于审核/待验货态才能终审", current.status
        )

    if to_status == COMPLETED_STATUS:
        session.flush()
        # 只有全部实付都退完(残额 <= 0)才推 order.refunded;部分退款保持 delivered。
        if residual_amount(session, refund.order_id) <= 0:
            refund_complete(session, refund.order_id)  # 内部 commit(退款单 + 订单原子)
            return session.get(RefundRequest, refund_id)
        session.commit()
        return session.get(RefundRequest, refund_id)

    if summary_text:
        _append_ticket_summary(session, refund.order_id, summary_text)
    session.commit()
    return session.get(RefundRequest, refund_id)


def mark_refunded(session: Session, refund_id: str) -> RefundRequest:
    """退款完成:审核/待验货 → 退款;残额为 0 时联动订单 delivered → refunded。"""
    return _apply_final(session, refund_id, COMPLETED_STATUS)


def mark_shipping(
    session: Session, refund_id: str, tracking_no: str
) -> RefundRequest:
    """买家回填物流单号:审核 → 退货中。"""
    result = session.execute(
        update(RefundRequest)
        .where(
            RefundRequest.id == refund_id,
            RefundRequest.status == "审核",
        )
        .values(status="退货中", tracking_no=tracking_no)
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(RefundRequest, refund_id)
        if current is None:
            raise NotFoundError(f"退款单不存在: {refund_id}")
        raise StateConflictError(
            "退款单需处于审核态才能回填物流", current.status
        )
    session.commit()
    return session.get(RefundRequest, refund_id)


def mark_received(session: Session, refund_id: str) -> RefundRequest:
    """仓库收货确认:退货中 → 待验货。"""
    result = session.execute(
        update(RefundRequest)
        .where(
            RefundRequest.id == refund_id,
            RefundRequest.status == "退货中",
        )
        .values(status="待验货")
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(RefundRequest, refund_id)
        if current is None:
            raise NotFoundError(f"退款单不存在: {refund_id}")
        raise StateConflictError("退款单需处于退货中才能确认收货", current.status)
    session.commit()
    return session.get(RefundRequest, refund_id)


def inspect(
    session: Session,
    refund_id: str,
    *,
    passed: bool,
    decided_by: str = "warehouse",
    reason: str | None = None,
) -> RefundRequest:
    """验货终审:审核/待验货 → 退款(passed)/驳回(不通过)。"""
    if passed:
        return _apply_final(session, refund_id, COMPLETED_STATUS, decided_by)
    text = f"[验货驳回] {reason.strip()}" if reason and reason.strip() else "[验货驳回] 未通过验货"
    return _apply_final(
        session, refund_id, REJECTED_STATUS, decided_by, summary_text=text
    )
