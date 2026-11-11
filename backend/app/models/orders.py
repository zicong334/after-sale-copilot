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
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    """退款申请单(新增,幂等台账)。三张核心表之一,约束全部照 DDL 落库。

    W11 状态机(唯一合法迁移,其余一律拒绝):

    ==========  ==========  ==========================================
    源状态       目标状态    触发方
    ==========  ==========  ==========================================
    申请        审核        商家审核通过(approve)
    申请        驳回        商家审核驳回(reject)
    审核        退货中      买家回填物流单号(POST /api/c/refunds/{id}/shipping)
    退货中      待验货      仓库收货确认(POST /api/fulfillment/receipt)
    审核        退款        验货通过且无需退货(inspection passed)
    审核        驳回        验货不通过(inspection failed)
    待验货      退款        验货通过(inspection passed)
    待验货      驳回        验货不通过(inspection failed)
    ==========  ==========  ==========================================

    "退货中"/"待验货"为 W11-2 新增状态;子项金额见 RefundItem。
    """

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
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    #: 子项(部分退款折算,W11-1);删除退款单时级联删除子项。
    items: Mapped[list["RefundItem"]] = relationship(
        back_populates="refund_request",
        cascade="all, delete-orphan",
        order_by="RefundItem.created_at",
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_refund_amount_pos"),
        CheckConstraint(
            "reason IN ('质量问题','错发漏发','七天无理由','物流破损')",
            name="ck_refund_reason",
        ),
        CheckConstraint(
            "status IN ('申请','审核','退货中','待验货','退款','驳回')",
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
            # '退款' 是终态:已完成的退款不再占活跃名额,否则同一订单无法做
            # 第二次部分退款(实付残额>0 时正是 W11-1 要支持的场景)。
            sqlite_where=text("status IN ('申请','审核','退货中','待验货')"),
            postgresql_where=text("status IN ('申请','审核','退货中','待验货')"),
        ),
    )


class RefundItem(Base):
    """退款单子项(W11-1 部分退款折算)。

    一张退款单可含多条子项;退款单总额 = Σ line_cents = Σ qty × unit_cents。
    同一 order_item 在所有退款单上的累计退 qty 不得超过其购买 qty。
    """

    __tablename__ = "refund_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    refund_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_request.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    order_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("order_item.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    line_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    refund_request: Mapped["RefundRequest"] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_refund_item_qty_pos"),
        CheckConstraint("unit_cents >= 0", name="ck_refund_item_unit_nonneg"),
        CheckConstraint("line_cents >= 0", name="ck_refund_item_line_nonneg"),
        CheckConstraint(
            "line_cents = qty * unit_cents",
            name="ck_refund_item_line_consistent",
        ),
        Index(
            "uq_refund_item_unique",
            "refund_request_id",
            "order_item_id",
            unique=True,
        ),
    )
