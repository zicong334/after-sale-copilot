"""api/auth.py — W8 登录与身份查询(唯一签发 JWT 的入口)。

- ``POST /auth/login``:``{username, password}`` → HS256 JWT(2 小时)+ 用户摘要。
  密码只做 pbkdf2 哈希比对;错误统一 401 ``AUTH_INVALID``(不区分账号/密码,防枚举)。
- ``GET  /auth/me``:带 Bearer token 回读当前身份(前端刷新页面校验登录态用)。

⚠️ MOCK 边界:账号由 ``backend/scripts/seed.py`` 幂等写入演示数据(明文口令写在
``backend/README.md``「演示账号」节);生产接 IdP/OAuth2 时只替本文件与
``security.create_token/decode_token``。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import get_db
from app.errors import AppError
from app.models import AuthUser
from app.security import (
    Identity,
    create_token,
    current_identity,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录请求:只收账号口令,身份一律由服务端按账号绑定的角色签发。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _user_dict(user: AuthUser) -> dict:
    """对外用户摘要(绝不包含 password_hash)。"""
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "merchant_id": user.merchant_id,
        "customer_id": user.customer_id,
        "service_id": user.service_id,
    }


def _identity_of(user: AuthUser) -> Identity:
    return Identity(
        sub=user.id,
        role=user.role,
        merchant_id=user.merchant_id,
        customer_id=user.customer_id,
        service_id=user.service_id,
    )


@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict:
    """账号口令登录 → JWT(HS256,2 小时)。"""
    user = db.scalar(select(AuthUser).where(AuthUser.username == payload.username))
    # 账号不存在也走一遍假哈希比对,避免时序差泄露账号是否存在。
    stored = user.password_hash if user is not None else (
        "pbkdf2_sha256$200000$AAAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    password_ok = verify_password(payload.password, stored)
    if user is None or not password_ok:
        raise AppError(
            code="AUTH_INVALID",
            message="用户名或密码错误",
            status_code=401,
        )
    token = create_token(_identity_of(user))
    return {
        "ok": True,
        "token": token,
        "token_type": "bearer",
        "expires_in": 2 * 3600,
        "user": _user_dict(user),
    }


@router.get("/me")
def me(
    identity: Identity = Depends(current_identity),
    db: Session = Depends(get_db),
) -> dict:
    """回读当前登录身份(401 由中间件按 token 先拦截)。"""
    user = db.get(AuthUser, identity.sub)
    if user is None:
        raise AppError(code="AUTH_INVALID", message="账号不存在", status_code=401)
    return {"ok": True, "user": _user_dict(user)}
