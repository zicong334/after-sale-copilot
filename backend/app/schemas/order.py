"""订单请求/响应模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OrderCreate(BaseModel):
    """创建订单请求。金额整数分,非负。"""

    merchant_id: str
    customer_id: str
    total_cents: int = Field(ge=0, description="订单实付金额,整数分")
    currency: str = "CNY"


class OrderOut(BaseModel):
    """订单响应(读操作返回完整资源)。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    merchant_id: str
    customer_id: str
    status: str
    total_cents: int
    currency: str
    created_at: datetime
