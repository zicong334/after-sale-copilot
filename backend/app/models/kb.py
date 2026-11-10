"""kb 模块:knowledge_item(店铺规则知识库,版本化 + 状态灰度 + 稠密向量落表)。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base

# W9-3 条款生命周期(draft 草稿 → active 生效 → retired 退役)。
KB_STATUS_VALUES: tuple[str, ...] = ("draft", "active", "retired")
KB_STATUS_DRAFT = "draft"
KB_STATUS_ACTIVE = "active"
KB_STATUS_RETIRED = "retired"


class KnowledgeItem(Base):
    """知识库条目(改造自 kb_items.jsonl 落表)。

    - ``status``(W9-3 新增):draft 草稿不参与检索 / active 生效 / retired 退役
      (检索不可见,可 ``restore`` 回滚到 active)。
    - ``version``:条目自身版本号,内容变更时自增("1" → "2"),检索结果带版本。
    - ``embedding_model / dim`` 为可选向量模型标注;真实向量另存 ``kb_vector``。
    """

    __tablename__ = "knowledge_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    clause_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=KB_STATUS_ACTIVE,
        server_default=KB_STATUS_ACTIVE, index=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, onupdate=utc_now, server_default=func.current_timestamp(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class KbVector(Base):
    """条款稠密向量(迁移 0007;W9-1 落表,W9-2 混合检索消费)。

    一条 active 条款一行;``knowledge_item_id`` 唯一,重建时按条款 upsert。
    向量由 ``TfidfSvdVectorBackend.export_vectors`` 生成(float32 小端),
    生产环境换 pgvector / Milvus 时本表可退役(影子表保留做回滚)。
    """

    __tablename__ = "kb_vector"
    __table_args__ = (
        UniqueConstraint("knowledge_item_id", name="uq_kb_vector_item"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    knowledge_item_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_item.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vec: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, onupdate=utc_now, server_default=func.current_timestamp(),
    )

