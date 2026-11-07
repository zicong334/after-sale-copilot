"""catalog 模块:product / product_variant / product_metric / review。"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base


class Product(Base):
    """商品(改造自归档 products)。金额整数分,商品级价格只作展示,真实库存走 SKU。"""

    __tablename__ = "product"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, onupdate=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_product_price_nonneg"),
    )


class ProductVariant(Base):
    """商品 SKU 变体(新增)。库存必须在 SKU 维度,否则"黑色 M 还有吗"回答不了。

    不带 merchant_id 的理由:纯从属表,经 product.merchant_id 可推导,避免冗余且
    永不出现变体与商品租户不一致的脏数据。
    """

    __tablename__ = "product_variant"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    color: Mapped[str | None] = mapped_column(String(50), nullable=True)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("stock >= 0", name="ck_variant_stock_nonneg"),
    )


class ProductMetric(Base):
    """商品指标(改造自 product_metrics)。(product_id, date) 复合主键,date 用 DATE。

    不带 merchant_id 的理由:纯从属表,经 product.merchant_id 可推导。
    """

    __tablename__ = "product_metric"

    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product.id", ondelete="CASCADE"),
        primary_key=True,
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("views >= 0", name="ck_pmetric_views_nonneg"),
        CheckConstraint("orders_count >= 0", name="ck_pmetric_orders_nonneg"),
        CheckConstraint("revenue_cents >= 0", name="ck_pmetric_revenue_nonneg"),
    )


class Review(Base):
    """商品评价(改造自 reviews)。order_id 追溯订单;sentiment 可空(没分析时别猜)。"""

    __tablename__ = "review"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("order.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_review_rating_range"),
        CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('positive','negative','neutral')",
            name="ck_review_sentiment",
        ),
    )
