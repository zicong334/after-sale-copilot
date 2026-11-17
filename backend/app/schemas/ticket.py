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
    """客服领单请求。

    A1(2026-11-16):``service_id`` **不再具有身份效力**——enforce 模式下领单坐席
    一律取 token 主体(见 ``api/tickets._service_id``),防止冒充他人领单。
    字段保留为可选,仅为兼容既有调用方与 internal 回归脚本;传了也会被忽略。
    """

    service_id: str | None = None


class TicketMessageIn(BaseModel):
    """工单消息请求。author_type 与模型 CHECK 一致。"""

    author_type: Literal["user", "agent", "system"]
    content: str
    is_internal: bool = False


class TicketOut(BaseModel):
    """工单响应。

    W11 B2:详情额外带该订单最新退款单 id/状态(无关联退款单时为 None),
    工作台据此显示"退款中/待验货"等履约进度。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    merchant_id: str
    customer_id: str
    order_id: str | None
    status: str
    claimed_by: str | None
    last_msg_at: datetime | None
    created_at: datetime
    refund_request_id: str | None = None
    refund_status: str | None = None


class TicketStatusIn(BaseModel):
    """W11 B7:工单状态手动流转请求。

    仅允许正向链路 ``queued → claimed → waiting_customer → closed``,
    任何回退/跳跃由服务层守卫拒绝(409)。
    """

    model_config = ConfigDict(extra="forbid")

    to_status: Literal["claimed", "waiting_customer", "closed"]
    service_id: str | None = None


class TicketMessageOut(BaseModel):
    """工单消息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    ticket_id: str
    author_type: str
    content: str
    is_internal: bool
    created_at: datetime
