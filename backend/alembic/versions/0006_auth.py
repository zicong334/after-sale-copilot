"""0006_auth

W8 接入安全:新增 3 张表。

- ``auth_user``:登录账号(username 唯一 / password_hash 只存 pbkdf2 哈希 /
  role=buyer|agent,买家绑 customer_id、坐席绑 service_id,均归属 merchant_id)。
- ``request_nonce``:防重放 nonce(UNIQUE(subject, nonce) + created_at 供 TTL 清理)。
- ``token_usage``:chat 日 token 用量聚合(UNIQUE(subject, day),配额检查用)。

Revision ID: 0006_auth
Revises: 0005_ticket_session_id
Create Date: 2026-11-09 21:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006_auth'
down_revision: Union[str, None] = '0005_ticket_session_id'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'auth_user',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('merchant_id', sa.String(length=36), nullable=False),
        sa.Column('customer_id', sa.String(length=36), nullable=True),
        sa.Column('service_id', sa.String(length=36), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('buyer','agent')", name='ck_auth_user_role'),
        sa.CheckConstraint(
            "(role = 'buyer' AND customer_id IS NOT NULL AND service_id IS NULL)"
            " OR (role = 'agent' AND service_id IS NOT NULL AND customer_id IS NULL)",
            name='ck_auth_user_binding',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username', name='uq_auth_user_username'),
    )
    op.create_index(
        op.f('ix_auth_user_username'), 'auth_user', ['username'], unique=True
    )
    op.create_index(
        op.f('ix_auth_user_merchant_id'), 'auth_user', ['merchant_id'], unique=False
    )

    op.create_table(
        'request_nonce',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('subject', sa.String(length=64), nullable=False),
        sa.Column('nonce', sa.String(length=128), nullable=False),
        sa.Column('ts', sa.Integer(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'subject', 'nonce', name='uq_request_nonce_subject_nonce'
        ),
    )
    op.create_index(
        op.f('ix_request_nonce_subject'), 'request_nonce', ['subject'], unique=False
    )

    op.create_table(
        'token_usage',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('subject', sa.String(length=64), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('tokens_in', sa.Integer(), nullable=False),
        sa.Column('tokens_out', sa.Integer(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subject', 'day', name='uq_token_usage_subject_day'),
    )
    op.create_index(
        op.f('ix_token_usage_subject'), 'token_usage', ['subject'], unique=False
    )
    op.create_index(op.f('ix_token_usage_day'), 'token_usage', ['day'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_token_usage_day'), table_name='token_usage')
    op.drop_index(op.f('ix_token_usage_subject'), table_name='token_usage')
    op.drop_table('token_usage')

    op.drop_index(op.f('ix_request_nonce_subject'), table_name='request_nonce')
    op.drop_table('request_nonce')

    op.drop_index(op.f('ix_auth_user_merchant_id'), table_name='auth_user')
    op.drop_index(op.f('ix_auth_user_username'), table_name='auth_user')
    op.drop_table('auth_user')
