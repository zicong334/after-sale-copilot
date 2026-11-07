"""merchant 租户根表:所有业务表的租户根。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base


class Merchant(Base):
    """店铺/商户。

    单店项目也保留 merchant_id 做租户根:所有查询从第一天就带租户条件,
    将来加店不用回填 schema,也不给自己留跨店串读的债。
    """

    __tablename__ = "merchant"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    locale: Mapped[str] = mapped_column(String(20), nullable=False, default="zh-CN")
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    tz: Mapped[str] = mapped_column(String(50), nullable=False, default="UTC")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )
