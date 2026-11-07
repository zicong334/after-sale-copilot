"""工单请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class TicketCreate(BaseModel):
    """创建工单请求。"""

    merchant_id: str
    customer_id: str
    order_id: str | None = None


class TicketClaim(BaseModel):
    """客服领单请求(claimed_by = 客服 id)。"""

    service_id: str


class TicketMessageIn(BaseModel):
    """工单消息请求。author_type 与模型 CHECK 一致。"""

    author_type: Literal["user", "agent", "system"]
    content: str
    is_internal: bool = False


class TicketOut(BaseModel):
    """工单响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    merchant_id: str
    customer_id: str
    order_id: str | None
    status: str
    claimed_by: str | None
    last_msg_at: datetime | None
    created_at: datetime


class TicketMessageOut(BaseModel):
    """工单消息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    ticket_id: str
    author_type: str
    content: str
    is_internal: bool
    created_at: datetime
