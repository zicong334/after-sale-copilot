"""W11-2 仓库履约回调:验货结论 / 收货确认。

鉴权不复用 JWT,而是 ``X-Signature = sha256(raw_body + FULFILLMENT_SECRET)``
(任务书原文公式)。secret 来自环境变量 ``FULFILLMENT_SECRET``;开发/测试未配置
时使用带 MOCK 标注的默认值——生产必须显式配置。

重放防护:请求体必须带 ``nonce``(>=8 位),同一 nonce 只能消费一次(落
``request_nonce`` 表,subject='fulfillment'),重放返回 409。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import AppError
from app.models import RequestNonce
from app.schemas import Receipt
from app.services import refunds as refund_svc

router = APIRouter(prefix="/api/fulfillment", tags=["fulfillment"])

#: MOCK:开发/测试默认 secret;生产必须用环境变量覆盖(不回显到响应里)。
DEV_FULFILLMENT_SECRET = "dev-w11-fulfillment-secret-change-me"
FULFILLMENT_SECRET_ENV = "FULFILLMENT_SECRET"
_REPLAY_SUBJECT = "fulfillment"
_NONCE_MIN_LEN = 8
_NONCE_MAX_LEN = 128


def fulfillment_secret() -> str:
    """读取回调 secret(每次读取,便于测试 monkeypatch 环境变量)。"""
    return os.getenv(FULFILLMENT_SECRET_ENV) or DEV_FULFILLMENT_SECRET


def compute_signature(raw_body: bytes, secret: str | None = None) -> str:
    """按任务书公式计算签名:hex(sha256(raw_body + secret))。"""
    key = (secret if secret is not None else fulfillment_secret()).encode("utf-8")
    return hashlib.sha256(raw_body + key).hexdigest()


def _verify_signature(request: Request, raw_body: bytes) -> None:
    provided = (request.headers.get("X-Signature") or "").strip().lower()
    expected = compute_signature(raw_body)
    if not provided or not hmac.compare_digest(provided, expected):
        raise AppError(
            "FULFILLMENT_SIGNATURE_INVALID",
            "回调签名校验失败",
            status_code=status.HTTP_401_UNAUTHORIZED,
            param="X-Signature",
        )


def _consume_nonce(db: Session, nonce: str) -> None:
    """nonce 一次性消费;重复/非法直接拒绝(412? -> 409 与全站冲突语义一致)。"""
    value = (nonce or "").strip()
    if len(value) < _NONCE_MIN_LEN or len(value) > _NONCE_MAX_LEN:
        raise AppError(
            "VALIDATION_ERROR",
            f"nonce 长度必须在 {_NONCE_MIN_LEN}-{_NONCE_MAX_LEN} 之间",
            status_code=400,
            param="nonce",
        )
    db.add(RequestNonce(subject=_REPLAY_SUBJECT, nonce=value, ts=int(time.time())))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(
            "REPLAY_DETECTED",
            "回调重放已拒绝",
            status_code=status.HTTP_409_CONFLICT,
            param="nonce",
        ) from None


class InspectionIn(BaseModel):
    """验货结论回调体。"""

    refund_id: str = Field(min_length=1, max_length=64)
    passed: bool
    reason: str | None = Field(default=None, max_length=200)
    nonce: str = Field(min_length=_NONCE_MIN_LEN, max_length=_NONCE_MAX_LEN)


class ReceiptIn(BaseModel):
    """仓库收货确认回调体(退货中 → 待验货)。"""

    refund_id: str = Field(min_length=1, max_length=64)
    nonce: str = Field(min_length=_NONCE_MIN_LEN, max_length=_NONCE_MAX_LEN)


async def _verified_body(request: Request) -> bytes:
    """读原始 body 并验签(签名覆盖调用方实际发送的字节)。"""
    raw_body = await request.body()
    _verify_signature(request, raw_body)
    return raw_body


@router.post("/inspection", response_model=Receipt)
async def inspection(
    request: Request,
    payload: InspectionIn,
    db: Session = Depends(get_db),
) -> Receipt:
    """验货回调:passed=True 审核/待验货 → 退款;False → 驳回并更新工单摘要。"""
    await _verified_body(request)
    _consume_nonce(db, payload.nonce)
    refund = refund_svc.inspect(
        db,
        payload.refund_id,
        passed=payload.passed,
        decided_by="warehouse",
        reason=payload.reason,
    )
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/receipt", response_model=Receipt)
async def receipt(
    request: Request,
    payload: ReceiptIn,
    db: Session = Depends(get_db),
) -> Receipt:
    """仓库收货确认:退货中 → 待验货。"""
    await _verified_body(request)
    _consume_nonce(db, payload.nonce)
    refund = refund_svc.mark_received(db, payload.refund_id)
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )
