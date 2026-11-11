"""W11-2 买家侧履约路由:退款单回填物流单号。

挂在 ``/api/c`` 下,由 ``AuthMiddleware`` 强制 buyer 角色;非 internal 模式下
只允许操作自己订单的退款单(越权按 404 处理,不泄露他单是否存在)。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import NotFoundError
from app.models import Order, RefundRequest
from app.schemas import Receipt
from app.security import Identity, current_identity
from app.services import refunds as refund_svc

router = APIRouter(prefix="/api/c/refunds", tags=["refunds-buyer"])

#: 物流单号格式:字母/数字/连字符,6-64 位(顺丰/中通/京东等主流单号均满足)。
TRACKING_NO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{5,63}$"


class ShippingIn(BaseModel):
    """买家回填物流单号。"""

    tracking_no: str = Field(pattern=TRACKING_NO_PATTERN)


def _scoped_refund(db: Session, refund_id: str, identity: Identity) -> RefundRequest:
    """取退款单并校验买家归属;internal 模式不校验。"""
    refund = db.get(RefundRequest, refund_id)
    if refund is None:
        raise NotFoundError(f"退款单不存在: {refund_id}")
    if identity.is_internal:
        return refund
    order = db.get(Order, refund.order_id)
    same_customer = (
        not identity.customer_id or (order is not None and order.customer_id == identity.customer_id)
    )
    same_merchant = (
        not identity.merchant_id or refund.merchant_id == identity.merchant_id
    )
    if not (same_customer and same_merchant):
        raise NotFoundError(f"退款单不存在: {refund_id}")
    return refund


@router.post("/{refund_id}/shipping", response_model=Receipt)
def submit_shipping(
    refund_id: str,
    payload: ShippingIn,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> Receipt:
    """买家回填退货物流单号:审核 → 退货中(守卫式,重复提交/非审核态 409)。"""
    _scoped_refund(db, refund_id, identity)
    refund = refund_svc.mark_shipping(db, refund_id, payload.tracking_no)
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )
