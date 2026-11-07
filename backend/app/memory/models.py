"""memory 表(第 15 张):买家长期记忆。

字段设计见任务书 W3-B:
- ``type`` 四类型(fact / episode / preference / procedure),白名单见 whitelist.py;
- ``source`` 三来源(user_stated / order_derived / agent_inferred),可审计;
- ``executable`` 与 ``status`` 是定价指令隔离的落点:涉及价格/折扣/承诺的记忆
  只能以 ``status='quarantined' + executable=False`` 落库,注入时永远查不到;
- ``session_id`` 为 NULL 表示长期记忆(跨会话);非 NULL 表示绑定某个会话;
- ``ttl_expires_at`` / ``decay_score`` / ``deleted_at`` 支撑过期、衰减与软删除。

数据层三铁律:时间一律 UTC(``utc_now``)、状态有 CHECK、主键走 ``gen_id``。
本表没有金额字段,故不涉及"金额整数分"。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base


class Memory(Base):
    """买家长期记忆(偏好/事实/片段/流程),按 merchant + user 维度隔离。"""

    __tablename__ = "memory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    merchant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    #: NULL = 长期记忆(跨会话);非 NULL = 绑定该 chat_session 的短期片段
    session_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("chat_session.id", ondelete="SET NULL"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    #: 保护权重:> IMPORTANCE_PROTECT 的记忆不参与衰减
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    #: 定价指令隔离:False 的记忆绝不进入注入/可执行查询
    executable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    scope: Mapped[str] = mapped_column(String(50), nullable=False, default="after_sale")
    ttl_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decay_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('fact','episode','preference','procedure')",
            name="ck_memory_type",
        ),
        CheckConstraint(
            "source IN ('user_stated','order_derived','agent_inferred')",
            name="ck_memory_source",
        ),
        CheckConstraint(
            "status IN ('active','quarantined','expired')",
            name="ck_memory_status",
        ),
        CheckConstraint(
            "importance >= 0 AND importance <= 1",
            name="ck_memory_importance",
        ),
        CheckConstraint(
            "decay_score >= 0 AND decay_score <= 1",
            name="ck_memory_decay_score",
        ),
        # 查询主路径:merchant + user + 未软删,再按 scope/status 过滤
        Index(
            "ix_memory_merchant_user_deleted",
            "merchant_id",
            "user_id",
            "deleted_at",
        ),
    )
