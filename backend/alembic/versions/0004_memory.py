"""0004_memory

新增第 15 张表 ``memory``(买家长期记忆):类型/来源/状态三组 CHECK,
``executable`` 与 ``status`` 支撑定价指令隔离,``ttl_expires_at`` / ``decay_score``
支撑过期与衰减,``deleted_at`` 软删除。

Revision ID: 0004_memory
Revises: 0003_ticket_summary
Create Date: 2026-11-07 18:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004_memory'
down_revision: Union[str, None] = '0003_ticket_summary'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'memory',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('merchant_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('session_id', sa.String(length=36), nullable=True),
        sa.Column('type', sa.String(length=20), nullable=False),
        sa.Column('importance', sa.Float(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('executable', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('scope', sa.String(length=50), nullable=False),
        sa.Column('ttl_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decay_score', sa.Float(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('fact','episode','preference','procedure')",
            name='ck_memory_type',
        ),
        sa.CheckConstraint(
            "source IN ('user_stated','order_derived','agent_inferred')",
            name='ck_memory_source',
        ),
        sa.CheckConstraint(
            "status IN ('active','quarantined','expired')",
            name='ck_memory_status',
        ),
        sa.CheckConstraint(
            'importance >= 0 AND importance <= 1',
            name='ck_memory_importance',
        ),
        sa.CheckConstraint(
            'decay_score >= 0 AND decay_score <= 1',
            name='ck_memory_decay_score',
        ),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchant.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['session_id'], ['chat_session.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_memory_merchant_id'), 'memory', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_memory_user_id'), 'memory', ['user_id'], unique=False)
    op.create_index(
        'ix_memory_merchant_user_deleted',
        'memory',
        ['merchant_id', 'user_id', 'deleted_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_memory_merchant_user_deleted', table_name='memory')
    op.drop_index(op.f('ix_memory_user_id'), table_name='memory')
    op.drop_index(op.f('ix_memory_merchant_id'), table_name='memory')
    op.drop_table('memory')
