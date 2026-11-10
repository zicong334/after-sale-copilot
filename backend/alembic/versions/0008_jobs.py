"""0008_jobs

W10-3 异步任务队列:新建 ``job_queue``(作业表)与 ``turn_audit``(审计落盘表)。

- ``job_queue``:一行 = 一个待办作业。``status`` 用 CHECK 约束限定为
  ``pending / running / done / dead``(``dead`` 即 dead-letter 行,重试耗尽后
  保留不删);``attempts`` 是已尝试次数,``max_attempts`` 默认 3
  (= 首次 + 重试 2 次);``payload`` 存 JSON 文本;时间列一律 UTC。
- ``turn_audit``:每轮对话的审计留痕,``citations`` 存 JSON 文本。

幂等性(与 0006 / 0007 同一套约定)
----------------------------------
``app/main.py`` lifespan 有 ``Base.metadata.create_all`` 兜底建表,所以迁移必须
对"表已存在"的库安全:先 ``sa.inspect`` 再决定 create/drop,重复执行不报错。

Revision ID: 0008_jobs
Revises: 0007_kb_vectors
Create Date: 2026-11-10 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008_jobs'
down_revision: Union[str, None] = '0007_kb_vectors'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector: sa.Inspector, table: str) -> bool:
    """表是否已存在(跨 SQLite 版本的稳定写法)。"""
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


def _ensure_index(
    table: str, index_name: str, columns: list[str], *, unique: bool = False
) -> None:
    """索引不存在才建(已存在则跳过)。"""
    inspector = sa.inspect(op.get_bind())
    if _has_index(inspector, table, index_name):
        return
    op.create_index(op.f(index_name), table, columns, unique=unique)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # 1) job_queue —— W10-3 作业表
    if not _has_table(inspector, "job_queue"):
        op.create_table(
            "job_queue",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint(
                "status IN ('pending','running','done','dead')",
                name="ck_job_queue_status",
            ),
            sa.CheckConstraint(
                "attempts >= 0 AND max_attempts >= 1",
                name="ck_job_queue_attempts",
            ),
        )
    _ensure_index("job_queue", "ix_job_queue_kind", ["kind"])
    _ensure_index("job_queue", "ix_job_queue_status", ["status"])
    _ensure_index("job_queue", "ix_job_queue_status_created", ["status", "created_at"])

    # 2) turn_audit —— 每轮对话审计留痕
    if not _has_table(inspector, "turn_audit"):
        op.create_table(
            "turn_audit",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("trace_id", sa.String(length=64), nullable=False),
            sa.Column("merchant_id", sa.String(length=36), nullable=False),
            sa.Column("customer_id", sa.String(length=36), nullable=False),
            sa.Column(
                "intent", sa.String(length=32), nullable=False, server_default=""
            ),
            sa.Column("reply", sa.Text(), nullable=True),
            sa.Column("citations", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("turn_audit", "ix_turn_audit_trace_id", ["trace_id"])
    _ensure_index("turn_audit", "ix_turn_audit_merchant_id", ["merchant_id"])
    _ensure_index(
        "turn_audit", "ix_turn_audit_trace_created", ["trace_id", "created_at"]
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "turn_audit"):
        op.drop_table("turn_audit")
    if _has_table(inspector, "job_queue"):
        op.drop_table("job_queue")
