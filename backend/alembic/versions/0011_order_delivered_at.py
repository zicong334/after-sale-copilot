"""0011_order_delivered_at

F1(2026-11-18 第四轮审计):订单补**签收时间** ``order.delivered_at``。

背景
----
七天无理由(RP-01,7 天)、质量问题(RP-03,15 天)等政策窗口都从**签收**起算,
但 ``order`` 表原先只有 ``created_at``。``graph/nodes.py`` 与
``orchestrator/w1_router.py`` 的 ``_days_since_delivery`` 只能拿下单时间兜底,
而下单 ≤ 签收 ⇒ **系统性高估签收天数 ⇒ 误拒正当退货**。
实测:10 天前下单、今天签收的订单被判「已超 7 天期限,原则上不予受理」;
同一张订单显式给 ``days_since_delivery=0`` 立刻受理 —— 缺陷精确落在兜底锚点。

口径
----
- 可空。历史数据与非签收态订单没有该值,**不回填臆造值**。
- 缺失时下游**不臆断天数**,转人工核实物流签收状态(不是放行,也不是拒绝)。
- 新值由 ``services/orders.deliver`` 在 shipped → delivered 的同一条 UPDATE 里写入。

幂等性(与 0006-0010 同一套约定)
--------------------------------
``app/main.py`` lifespan 有 ``Base.metadata.create_all`` 兜底建表,所以迁移必须
对"列已存在"的库安全:先 ``sa.inspect`` 再决定加不加,重复执行不报错。

Revision ID: 0011_order_delivered_at
Revises: 0010_badcase
Create Date: 2026-11-18 11:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011_order_delivered_at'
down_revision: Union[str, None] = '0010_badcase'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    """列是否已存在(表不存在时一律 False)。"""
    if not inspector.has_table(table):
        return False
    try:
        columns = inspector.get_columns(table)
    except sa.exc.NoSuchTableError:  # pragma: no cover - 竞态兜底
        return False
    return any(item.get("name") == column for item in columns)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "order", "delivered_at"):
        return
    op.add_column(
        "order",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, "order", "delivered_at"):
        return
    # SQLite 不支持直接 DROP COLUMN,用 batch 模式重建表(Alembic 官方做法)。
    with op.batch_alter_table("order") as batch:
        batch.drop_column("delivered_at")