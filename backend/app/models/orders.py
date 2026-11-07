"""orders 模块:order / order_item / refund_request。

"order" 是 SQL 保留字,SQLAlchemy 会自动加引号生成 `CREATE TABLE "order"`。
三张核心表(order/refund_request)的 CHECK/UNIQUE/索引照 03§4.1.3 DDL 原样落。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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


class Order(Base):
    """订单(新增,当前最大的实体缺口)。金额整数分;状态 CHECK 挡住非法值。"""

    __tablename__ = "order"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    customer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="created",
    )
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('created','paid','shipped','delivered','closed','refunded')",
            name="ck_order_status",
        ),
        CheckConstraint("total_cents >= 0", name="ck_order_total_nonneg"),
    )


class OrderItem(Base):
    """订单项(新增)。unit_price 用整数分。

    不带 merchant_id 的理由:纯从属表,经 order.merchant_id 可推导。
    """

    __tablename__ = "order_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("order.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variant.id", ondelete="SET NULL"),
        nullable=True,
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_orderitem_qty_pos"),
        CheckConstraint("unit_price_cents >= 0", name="ck_orderitem_price_nonneg"),
    )


class RefundRequest(Base):
    """退款申请单(新增,幂等台账)。三张核心表之一,约束全部照 DDL 落库。"""

    __tablename__ = "refund_request"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("order.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="申请")
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True,
    )
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_refund_amount_pos"),
        CheckConstraint(
            "reason IN ('质量问题','错发漏发','七天无理由','物流破损')",
            name="ck_refund_reason",
        ),
        CheckConstraint(
            "status IN ('申请','审核','退款','驳回')",
            name="ck_refund_status",
        ),
        CheckConstraint(
            "(status = '申请') = (decided_by IS NULL)",
            name="ck_refund_invariant",
        ),
        Index(
            "uq_refund_active",
            "order_id",
            unique=True,
            sqlite_where=text("status IN ('申请','审核','退款')"),
            postgresql_where=text("status IN ('申请','审核','退款')"),
        ),
    )
