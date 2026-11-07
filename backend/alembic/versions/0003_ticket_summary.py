"""0003_ticket_summary

给 ticket 表补独立 ``summary`` 列(缺陷修复包 F3;G7 设计要求工单摘要独立存储,
此前 summary 权宜折进 ``handoff_reason``)。可空列,存量工单不需要回填。

Revision ID: 0003_ticket_summary
Revises: 0002_add_tool_events
Create Date: 2026-11-07 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003_ticket_summary'
down_revision: Union[str, None] = '0002_add_tool_events'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ticket', sa.Column('summary', sa.Text(), nullable=True))


def downgrade() -> None:
    # SQLite 用 batch 模式重建表,保证 drop_column 在各版本 SQLite 下都可用
    with op.batch_alter_table('ticket') as batch_op:
        batch_op.drop_column('summary')
