"""0007_kb_vectors

W9 混合检索与知识动态化:knowledge_item 加 ``status`` + 新表 ``kb_vector``。

- ``knowledge_item.status``(String20, NOT NULL, default 'active'):条款生命周期
  ``draft / active / retired``;检索只取 active,退役条款仍可 ``restore`` 回滚。
- ``kb_vector``:条款稠密向量(W9-1)。
  ``knowledge_item_id`` UNIQUE + FK CASCADE,``vec`` BLOB(float32 小端),
  ``model`` / ``dim`` 记录生成模型与实际维数;``updated_at`` 供重建审计。

幂等性(与 0006 同一套约定)
--------------------------
``app/main.py`` lifespan 有 ``Base.metadata.create_all`` 兜底建表,所以迁移必须
对"表/列已存在"的库安全:先 ``sa.inspect`` 再决定 create/add,重复执行不报错。

Revision ID: 0007_kb_vectors
Revises: 0006_auth
Create Date: 2026-11-10 11:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007_kb_vectors'
down_revision: Union[str, None] = '0006_auth'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector: sa.Inspector, table: str) -> bool:
    """表是否已存在(跨 SQLite 版本的稳定写法)。"""
    return inspector.has_table(table)


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    """列是否已存在(表不存在时一律 False)。"""
    if not _has_table(inspector, table):
        return False
    try:
        columns = inspector.get_columns(table)
    except sa.exc.NoSuchTableError:  # pragma: no cover - 竞态兜底
        return False
    return any(item.get("name") == column for item in columns)


def _has_index(inspector: sa.Inspector, table: str, index_name: str) -> bool:
    """索引是否已存在(表不存在时一律 False)。"""
    if not _has_table(inspector, table):
        return False
    try:
        indexes = inspector.get_indexes(table)
    except sa.exc.NoSuchTableError:  # pragma: no cover - 竞态兜底
        return False
    return any(item.get("name") == index_name for item in indexes)


def _ensure_index(
    table: str, index_name: str, columns: list[str], *, unique: bool
) -> None:
    """索引不存在才建(已存在则跳过)。"""
    inspector = sa.inspect(op.get_bind())
    if _has_index(inspector, table, index_name):
        return
    op.create_index(op.f(index_name), table, columns, unique=unique)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # 1) knowledge_item.status —— 幂等加列(带 server_default,历史行落 active)
    if not _has_column(inspector, "knowledge_item", "status"):
        op.add_column(
            "knowledge_item",
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="active",
            ),
        )
    _ensure_index(
        "knowledge_item", "ix_knowledge_item_status", ["status"], unique=False
    )

    # 2) kb_vector —— W9-1 稠密向量落表
    if not _has_table(inspector, "kb_vector"):
        op.create_table(
            "kb_vector",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("knowledge_item_id", sa.String(length=36), nullable=False),
            sa.Column("vec", sa.LargeBinary(), nullable=False),
            sa.Column("model", sa.String(length=100), nullable=False),
            sa.Column("dim", sa.Integer(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.ForeignKeyConstraint(
                ["knowledge_item_id"],
                ["knowledge_item.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "knowledge_item_id", name="uq_kb_vector_item"
            ),
        )
    _ensure_index(
        "kb_vector", "ix_kb_vector_item", ["knowledge_item_id"], unique=False
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "kb_vector"):
        op.drop_table("kb_vector")
    # SQLite 的 batch 重建会反射表上的索引定义;若先 drop 列,重建索引时
    # CREATE INDEX ... (status) 会因列已消失报 "no such column: status"。
    # 因此必须先删索引,再删列。
    if _has_index(inspector, "knowledge_item", "ix_knowledge_item_status"):
        op.drop_index("ix_knowledge_item_status", table_name="knowledge_item")
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "knowledge_item", "status"):
        # SQLite 直接 drop column(3.35+);旧 SQLite 会报错,回滚以重建为准。
        with op.batch_alter_table("knowledge_item") as batch_op:
            batch_op.drop_column("status")
