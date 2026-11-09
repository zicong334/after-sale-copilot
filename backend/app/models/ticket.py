"""ticket 模块:ticket / ticket_message。ticket 是三张核心表之一,约束照 DDL。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base


class Ticket(Base):
    """工单(改造自 tickets)。状态机是核心:CHECK 把非法值挡在库外。"""

    __tablename__ = "ticket"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    customer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("order.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # 会话精确归属(W5-FIX R1):handoff 确认建单时写入当前 thread_id。
    # 可空是为了兼容 0005 之前的历史工单(那时 ticket 表没有会话列)。
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued",
    )
    claimed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    handoff_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_msg_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','claimed','waiting_customer','closed')",
            name="ck_ticket_status",
        ),
        # 不变量:queued 必须没有 claimed_by,claimed/waiting_customer 必须有;closed 除外
        CheckConstraint(
            "((status = 'queued') = (claimed_by IS NULL)) OR (status = 'closed')",
            name="ck_ticket_invariant",
        ),
        Index(
            "idx_ticket_merchant_status",
            "merchant_id",
            "status",
            text("last_msg_at DESC"),
        ),
    )


class TicketMessage(Base):
    """工单消息(改造自 ticket_messages)。author_type + is_internal。

    不带 merchant_id 的理由:纯从属表,经 ticket.merchant_id 可推导。
    """

    __tablename__ = "ticket_message"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ticket.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    author_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint(
            "author_type IN ('user','agent','system')",
            name="ck_ticket_msg_author_type",
        ),
    )
