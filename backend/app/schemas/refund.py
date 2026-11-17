"""退款申请请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RefundItemIn(BaseModel):
    """部分退款子项(W11-1):只给 qty,单价与行金额由服务端折算。"""

    order_item_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0, le=1000)


class RefundCreate(BaseModel):
    """创建退款申请请求。

    - 整单退款:``amount_cents`` 必填(旧行为);
    - 部分退款:传 ``items``,服务端按订单子项单价折算金额并忽略 ``amount_cents``。
    """

    merchant_id: str
    order_id: str
    amount_cents: int | None = Field(default=None, gt=0, description="退款金额,整数分")
    reason: Literal["质量问题", "错发漏发", "七天无理由", "物流破损"]
    idempotency_key: str
    items: list[RefundItemIn] | None = None

    @model_validator(mode="after")
    def _require_amount_or_items(self) -> "RefundCreate":
        if self.items is None and self.amount_cents is None:
            raise ValueError("amount_cents 与 items 至少提供一个")
        return self


class RefundDecision(BaseModel):
    """审核/驳回决策请求。

    A1(2026-11-16):``decided_by`` **不再具有身份效力**——enforce 模式下审核人
    一律取 token 主体(见 ``api/refunds._decided_by``),与 W13 对 ``merchant_id``
    的处理同一约定。字段保留为可选,仅为兼容既有调用方与 internal 回归脚本;
    传了也会被忽略,不传则完全合法。
    """

    decided_by: str | None = None


class RefundItemOut(BaseModel):
    """退款子项响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    order_item_id: str
    qty: int
    unit_cents: int
    line_cents: int


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
    tracking_no: str | None = None
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime
    items: list[RefundItemOut] = Field(default_factory=list)
