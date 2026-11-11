"""0010_badcase

W12-2 数据飞轮:坏例回流表 ``badcase``。

一行 = 坐席在工作台点「标记坏例」时的现场快照(trace + 转录 + 工具链 +
judge + 坐席修改 diff)。草稿由 ``scripts/promote_badcase.py`` 生成到
``data/eval/drafts/``,本迁移**不碰** ``data/eval/cases_v1.json``。

幂等性(与 0006-0009 同一套约定)
--------------------------------
``app/main.py`` lifespan 有 ``Base.metadata.create_all`` 兜底建表,所以迁移必须
对"表已存在 / 索引已存在"的库安全:先 ``sa.inspect`` 再决定建不建,重复执行不报错。

Revision ID: 0010_badcase
Revises: 0009_fulfillment
Create Date: 2026-11-12 00:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010_badcase'
down_revision: Union[str, None] = '0009_fulfillment'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector: sa.Inspector, table: str) -> bool:
    """表是否已存在(表不存在时一律 False)。"""
    return inspector.has_table(table)


def _has_index(inspector: sa.Inspector, table: str, index_name: str) -> bool:
    """索引是否已存在(表不存在时一律 False)。"""
    if not _has_table(inspector, table):
        return False
    try:
        indexes = inspector.get_indexes(table)
    except sa.exc.NoSuchTableError:  # pragma: no cover - 竞态兜底
        return False
    return any(item.get("name") == index_name for item in indexes)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "badcase"):
        op.create_table(
            "badcase",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("ticket_id", sa.String(length=36), nullable=False),
            sa.Column("merchant_id", sa.String(length=36), nullable=False),
            sa.Column("order_id", sa.String(length=36), nullable=True),
            sa.Column("handoff_reason", sa.String(length=20), nullable=True),
            sa.Column("trace_id", sa.String(length=64), nullable=True),
            sa.Column("service_id", sa.String(length=36), nullable=True),
            sa.Column("suggestion", sa.Text(), nullable=True),
            sa.Column("agent_final", sa.Text(), nullable=True),
            sa.Column("diff", sa.Text(), nullable=True),
            sa.Column("transcript", sa.Text(), nullable=False),
            sa.Column("tool_chain", sa.Text(), nullable=False),
            sa.Column("judge_score", sa.Float(), nullable=True),
            sa.Column("judge_detail", sa.Text(), nullable=True),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("eval_case_id", sa.String(length=100), nullable=True),
            sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "status IN ('new','promoted','ignored')",
                name="ck_badcase_status",
            ),
            sa.CheckConstraint(
                "judge_score IS NULL OR (judge_score >= 0 AND judge_score <= 1)",
                name="ck_badcase_judge_score",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    for index_name, columns in (
        ("ix_badcase_ticket_id", ["ticket_id"]),
        ("ix_badcase_merchant_id", ["merchant_id"]),
        ("ix_badcase_trace_id", ["trace_id"]),
        ("ix_badcase_status", ["status"]),
        ("ix_badcase_status_created", ["status", "created_at"]),
    ):
        if not _has_index(inspector, "badcase", index_name):
            op.create_index(index_name, "badcase", columns, unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "badcase"):
        op.drop_table("badcase")
