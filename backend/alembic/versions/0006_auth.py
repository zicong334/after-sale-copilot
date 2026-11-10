"""0006_auth

W8 接入安全:新增 3 张表。

- ``auth_user``:登录账号(username 唯一 / password_hash 只存 pbkdf2 哈希 /
  role=buyer|agent,买家绑 customer_id、坐席绑 service_id,均归属 merchant_id)。
- ``request_nonce``:防重放 nonce(UNIQUE(subject, nonce) + created_at 供 TTL 清理)。
- ``token_usage``:chat 日 token 用量聚合(UNIQUE(subject, day),配额检查用)。

幂等性(W8 收尾修复)
--------------------
迁移必须是**幂等**的:表/索引已存在时跳过,而不是报 ``OperationalError:
table auth_user already exists``。

原因:``backend/app/main.py`` 的 lifespan 里有 ``Base.metadata.create_all(bind=engine)``
兜底建表(无迁移环境也能跑)。当某个库先被 ``create_all`` 建过 0006 的表、
``alembic_version`` 还停在 0005 时,再跑 ``upgrade head`` 就会撞表。
因此本迁移对三种起点都安全:

1. 全新空库(0001→0006 链一次跑完);
2. 正常停在 0005 的库(0006 的表还不存在);
3. ``create_all`` 交叉污染过的库(表已存在但版本号停在 0005)。

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
    table: str, index_name: str, columns: list[str], *, unique: bool
) -> None:
    """索引不存在才建(已存在则跳过,保证迁移可重复执行)。"""
    inspector = sa.inspect(op.get_bind())
    if _has_index(inspector, table, index_name):
        return
    op.create_index(op.f(index_name), table, columns, unique=unique)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not _has_table(inspector, "auth_user"):
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
    _ensure_index(
        'auth_user', 'ix_auth_user_username', ['username'], unique=True
    )
    _ensure_index(
        'auth_user', 'ix_auth_user_merchant_id', ['merchant_id'], unique=False
    )

    if not _has_table(inspector, "request_nonce"):
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
    _ensure_index(
        'request_nonce', 'ix_request_nonce_subject', ['subject'], unique=False
    )

    if not _has_table(inspector, "token_usage"):
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
    _ensure_index('token_usage', 'ix_token_usage_subject', ['subject'], unique=False)
    _ensure_index('token_usage', 'ix_token_usage_day', ['day'], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if _has_table(inspector, "token_usage"):
        op.drop_index(op.f('ix_token_usage_day'), table_name='token_usage')
        op.drop_index(op.f('ix_token_usage_subject'), table_name='token_usage')
        op.drop_table('token_usage')

    if _has_table(inspector, "request_nonce"):
        op.drop_index(op.f('ix_request_nonce_subject'), table_name='request_nonce')
        op.drop_table('request_nonce')

    if _has_table(inspector, "auth_user"):
        op.drop_index(op.f('ix_auth_user_merchant_id'), table_name='auth_user')
        op.drop_index(op.f('ix_auth_user_username'), table_name='auth_user')
        op.drop_table('auth_user')
