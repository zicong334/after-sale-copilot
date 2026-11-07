"""0002_add_tool_events

新增 tool_events(append-only trace 事件)与 confirm_request(写操作人工确认卡)
两张表,匹配 ``app/tools/events.py`` 的 ToolEvent / ConfirmRequest 模型。

这是 W2-B 唯一允许的 models/alembic 改动(任务书白名单);不碰 0001_initial。

Revision ID: 0002_add_tool_events
Revises: 4b4528c62882
Create Date: 2026-11-07 14:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002_add_tool_events'
down_revision: Union[str, None] = '4b4528c62882'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('tool_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('type', sa.String(length=20), nullable=False),
    sa.Column('trace_id', sa.String(length=64), nullable=False),
    sa.Column('step', sa.Integer(), nullable=False),
    sa.Column('tool_name', sa.String(length=100), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('step >= 0', name='ck_tool_event_step_nonneg'),
    sa.CheckConstraint("type = 'tool_call'", name='ck_tool_event_type'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tool_events_trace_id'), 'tool_events', ['trace_id'], unique=False)

    op.create_table('confirm_request',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('confirm_token', sa.String(length=64), nullable=False),
    sa.Column('tool_name', sa.String(length=100), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('what', sa.Text(), nullable=False),
    sa.Column('impact', sa.Text(), nullable=False),
    sa.Column('irreversibility', sa.Text(), nullable=False),
    sa.Column('alternatives', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('pending','confirmed','expired','cancelled')", name='ck_confirm_request_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('confirm_token')
    )


def downgrade() -> None:
    op.drop_table('confirm_request')
    op.drop_index(op.f('ix_tool_events_trace_id'), table_name='tool_events')
    op.drop_table('tool_events')
