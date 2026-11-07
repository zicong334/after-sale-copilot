"""退款申请请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RefundCreate(BaseModel):
    """创建退款申请请求。amount_cents 必须为正整数分。"""

    merchant_id: str
    order_id: str
    amount_cents: int = Field(gt=0, description="退款金额,整数分")
    reason: Literal["质量问题", "错发漏发", "七天无理由", "物流破损"]
    idempotency_key: str


class RefundDecision(BaseModel):
    """审核/驳回决策请求(decided_by = 决策人 id)。"""

    decided_by: str


class RefundOut(BaseModel):
    """退款申请响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    merchant_id: str
    order_id: str
    amount_cents: int
    reason: str
    status: str
    idempotency_key: str
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime
