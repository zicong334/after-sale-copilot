"""0009_fulfillment

W11 售后履约纵深包的数据结构落地:

- ``refund_item``:退款单子项表(W11-1 部分退款折算)。一行 = 一条被退的订单子项,
  ``line_cents = qty × unit_cents``;退款单总额 = Σ line_cents。
- ``refund_request``:
  1. 新增 ``tracking_no`` 列(W11-2 买家回填物流单号);
  2. ``ck_refund_status`` 扩展出 ``退货中`` / ``待验货`` 两个状态
     (申请→审核→退货中→待验货→退款/驳回)。
- ``ticket``:新增 ``sla_breached`` 列(W11-3 队列 SLA 超时标记)。

SQLite 兼容性
-------------
SQLite 不支持 ``ALTER TABLE ... DROP CONSTRAINT``,改 CHECK 必须重建表。
本迁移在 SQLite 上走"建新表 → 拷数据 → 删旧表 → 改名 → 重建索引"的路径;
其它方言走 ``add_column`` + ``drop_constraint`` + ``create_check_constraint``。

幂等性(与 0006/0007/0008 同一套约定)
------------------------------------
``app/main.py`` lifespan 有 ``Base.metadata.create_all`` 兜底建表,所以迁移必须
对"表已存在 / 列已存在"的库安全:先 ``sa.inspect`` 再决定建不建,重复执行不报错。

Revision ID: 0009_fulfillment
Revises: 0008_jobs
Create Date: 2026-11-11 17:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009_fulfillment'
down_revision: Union[str, None] = '0008_jobs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_REFUND_STATUS_CHECK_NEW = (
    "status IN ('申请','审核','退货中','待验货','退款','驳回')"
)


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


def _rebuild_refund_request_sqlite() -> None:
    """SQLite:重建 refund_request 表以扩展 CHECK 并新增 tracking_no 列。"""
    bind = op.get_bind()
    op.create_table(
        "refund_request__w11_new",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("merchant_id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("decided_by", sa.String(length=36), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tracking_no", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("(status = '申请') = (decided_by IS NULL)", name="ck_refund_invariant"),
        sa.CheckConstraint(
            "reason IN ('质量问题','错发漏发','七天无理由','物流破损')",
            name="ck_refund_reason",
        ),
        sa.CheckConstraint(_REFUND_STATUS_CHECK_NEW, name="ck_refund_status"),
        sa.CheckConstraint("amount_cents > 0", name="ck_refund_amount_pos"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["order.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.execute(
        "INSERT INTO refund_request__w11_new "
        "(id, merchant_id, order_id, amount_cents, reason, status, idempotency_key, "
        "decided_by, decided_at, tracking_no, created_at) "
        "SELECT id, merchant_id, order_id, amount_cents, reason, status, idempotency_key, "
        "decided_by, decided_at, NULL, created_at FROM refund_request"
    )
    op.drop_table("refund_request")
    op.rename_table("refund_request__w11_new", "refund_request")
    op.create_index(op.f("ix_refund_request_merchant_id"), "refund_request", ["merchant_id"], unique=False)
    op.create_index(op.f("ix_refund_request_order_id"), "refund_request", ["order_id"], unique=False)
    op.create_index(
        "uq_refund_active",
        "refund_request",
        ["order_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('申请','审核','退款')"),
        postgresql_where=sa.text("status IN ('申请','审核','退款')"),
    )
    bind  # 保持 bind 引用,便于将来扩展


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    dialect = op.get_bind().dialect.name

    # 1) refund_request:tracking_no 列 + 扩展后的状态 CHECK
    if _has_table(inspector, "refund_request"):
        if dialect == "sqlite":
            _rebuild_refund_request_sqlite()
        else:  # pragma: no cover - 非 SQLite 方言分支
            if not _has_column(inspector, "refund_request", "tracking_no"):
                op.add_column(
                    "refund_request",
                    sa.Column("tracking_no", sa.String(length=64), nullable=True),
                )
            op.drop_constraint("ck_refund_status", "refund_request", type_="check")
            op.create_check_constraint(
                "ck_refund_status", "refund_request", _REFUND_STATUS_CHECK_NEW
            )

    # 2) refund_item 子项表(W11-1)
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "refund_item"):
        op.create_table(
            "refund_item",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("refund_request_id", sa.String(length=36), nullable=False),
            sa.Column("order_item_id", sa.String(length=36), nullable=False),
            sa.Column("qty", sa.Integer(), nullable=False),
            sa.Column("unit_cents", sa.Integer(), nullable=False),
            sa.Column("line_cents", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.current_timestamp(),
                nullable=False,
            ),
            sa.CheckConstraint("qty > 0", name="ck_refund_item_qty_pos"),
            sa.CheckConstraint("unit_cents >= 0", name="ck_refund_item_unit_nonneg"),
            sa.CheckConstraint("line_cents >= 0", name="ck_refund_item_line_nonneg"),
            sa.CheckConstraint(
                "line_cents = qty * unit_cents", name="ck_refund_item_line_consistent"
            ),
            sa.ForeignKeyConstraint(
                ["refund_request_id"], ["refund_request.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["order_item_id"], ["order_item.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    if not _has_index(inspector, "refund_item", "ix_refund_item_refund_request_id"):
        op.create_index(
            op.f("ix_refund_item_refund_request_id"),
            "refund_item",
            ["refund_request_id"],
            unique=False,
        )
    if not _has_index(inspector, "refund_item", "ix_refund_item_order_item_id"):
        op.create_index(
            op.f("ix_refund_item_order_item_id"),
            "refund_item",
            ["order_item_id"],
            unique=False,
        )
    if not _has_index(
        inspector, "refund_item", "uq_refund_item_unique"
    ):
        op.create_index(
            "uq_refund_item_unique",
            "refund_item",
            ["refund_request_id", "order_item_id"],
            unique=True,
        )

    # 3) ticket:sla_breached 列(W11-3)
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "ticket") and not _has_column(
        inspector, "ticket", "sla_breached"
    ):
        op.add_column(
            "ticket",
            sa.Column(
                "sla_breached",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "refund_item"):
        op.drop_table("refund_item")
    # refund_request 的 CHECK 回退与 tracking_no 列删除在 SQLite 上需要重建表,
    # 属于破坏性回退,此处仅做能安全做的部分(删子项表)。
