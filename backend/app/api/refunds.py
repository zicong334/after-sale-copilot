"""退款 API:创建申请(幂等)/审核/驳回/退款完成。

全部委托 services.refunds;创建申请带 idempotency_key 幂等,
重复提交命中返回已有单(idempotent_hit=true);金额超实付 422;活跃退款冲突 409。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import NotFoundError
from app.models import RefundRequest
from app.schemas import Receipt, RefundCreate, RefundDecision, RefundOut
from app.services import refunds as refund_svc

router = APIRouter(tags=["refunds"])


@router.post(
    "/refunds", response_model=Receipt, status_code=status.HTTP_201_CREATED
)
def create_refund(payload: RefundCreate, db: Session = Depends(get_db)) -> Receipt:
    refund, idempotent_hit = refund_svc.create_refund_request(
        db,
        merchant_id=payload.merchant_id,
        order_id=payload.order_id,
        amount_cents=payload.amount_cents,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
    )
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
        idempotent_hit=idempotent_hit,
    )


@router.get("/refunds/{refund_id}", response_model=RefundOut)
def get_refund(refund_id: str, db: Session = Depends(get_db)) -> RefundRequest:
    refund = db.get(RefundRequest, refund_id)
    if refund is None:
        raise NotFoundError(f"退款单不存在: {refund_id}")
    return refund


@router.post("/refunds/{refund_id}/approve", response_model=Receipt)
def approve(
    refund_id: str, payload: RefundDecision, db: Session = Depends(get_db)
) -> Receipt:
    refund = refund_svc.approve(db, refund_id, payload.decided_by)
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/refunds/{refund_id}/reject", response_model=Receipt)
def reject(
    refund_id: str, payload: RefundDecision, db: Session = Depends(get_db)
) -> Receipt:
    refund = refund_svc.reject(db, refund_id, payload.decided_by)
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/refunds/{refund_id}/refund", response_model=Receipt)
def mark_refunded(refund_id: str, db: Session = Depends(get_db)) -> Receipt:
    refund = refund_svc.mark_refunded(db, refund_id)
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )
