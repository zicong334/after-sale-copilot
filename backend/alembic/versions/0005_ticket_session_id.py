"""0005_ticket_session_id

给 ``ticket`` 增加可空 ``session_id`` 列 + 索引:把「会话 → 工单」从
merchant+customer 的启发式关联(取同客户"最近一张"工单)改为按会话精确归属。

背景(W5-FIX R1):旧逻辑下,同客户历史遗留的 closed 工单会被当成"当前会话的
工单",把刚发起的新会话误判为已结束(composer 禁用、send 消失、徽章错误)。
新会话建单时会写入 thread_id,买家页只认本会话的工单;历史工单该列为 NULL,
读取侧不回退客户级启发式。

Revision ID: 0005_ticket_session_id
Revises: 0004_memory
Create Date: 2026-11-09 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005_ticket_session_id'
down_revision: Union[str, None] = '0004_memory'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite 的 ALTER TABLE 能力有限,batch 模式会重建表以兼容 add_column + create_index。
    with op.batch_alter_table('ticket') as batch_op:
        batch_op.add_column(sa.Column('session_id', sa.String(length=36), nullable=True))
        batch_op.create_index('ix_ticket_session_id', ['session_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('ticket') as batch_op:
        batch_op.drop_index('ix_ticket_session_id')
        batch_op.drop_column('session_id')
