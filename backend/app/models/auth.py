"""auth 模块:auth_user / request_nonce / token_usage(W8 迁移 0006)。

- ``auth_user``:登录账号。密码只存 pbkdf2 哈希(``security.hash_password``);
  buyer 绑定 ``customer_id``,agent 绑定 ``service_id``,都归属某个 ``merchant_id``。
- ``request_nonce``:防重放 nonce 表。UNIQUE(subject, nonce) 兜住并发重放;
  ``created_at`` 供 TTL 清理。
- ``token_usage``:按 (subject, day) 聚合 chat 日 token 用量,供配额检查。
  独立成表而不是写 ``tool_events``:既有测试会断言 tool_events 行数。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base


class AuthUser(Base):
    """登录账号(演示账号由 backend/scripts/seed.py 幂等写入)。"""

    __tablename__ = "auth_user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    username: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("role IN ('buyer','agent')", name="ck_auth_user_role"),
        CheckConstraint(
            "(role = 'buyer' AND customer_id IS NOT NULL AND service_id IS NULL)"
            " OR (role = 'agent' AND service_id IS NOT NULL AND customer_id IS NULL)",
            name="ck_auth_user_binding",
        ),
    )


class RequestNonce(Base):
    """防重放 nonce:同一主体同一 nonce 只能落库一次。"""

    __tablename__ = "request_nonce"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )

    __table_args__ = (
        UniqueConstraint("subject", "nonce", name="uq_request_nonce_subject_nonce"),
    )


class TokenUsage(Base):
    """chat 日 token 用量(按主体 + UTC 日聚合)。"""

    __tablename__ = "token_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )

    __table_args__ = (
        UniqueConstraint("subject", "day", name="uq_token_usage_subject_day"),
    )
