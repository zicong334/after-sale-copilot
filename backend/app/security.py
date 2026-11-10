"""app/security.py — W8 接入安全与实时通讯基建(单文件)。

任务书 W8 只给了一个新基建模块(``backend/app/security.py``),因此本文件同时承载
五件事,每件事都以「可替换接口 + 本机真跑实现」两层给出(工业化缝):

1. **密码哈希**:``hashlib.pbkdf2_hmac``(标准库;requirements.lock 无 passlib/bcrypt)。
2. **JWT HS256**:手写(标准库 hmac/hashlib/base64;requirements.lock 无 pyjwt)。
   生产缝:换 IdP/OAuth2 时只替 ``create_token``/``decode_token`` 的校验来源。
3. **身份对象与依赖**:``Identity`` + ``current_identity``(FastAPI 依赖)。
4. **限流**:``RateLimiter`` ABC + ``MemoryRateLimiter``(令牌桶,用户级 5 QPS/
   突发 10,租户级 50 QPS)。生产缝:同接口换 Redis(INCR + EXPIRE)。
   chat 另有日 token 配额,按 ``token_usage`` 表 SQL 聚合(不写 tool_events,
   避免破坏既有事件条数断言)。
5. **防重放**:``X-Timestamp`` + ``X-Nonce``,窗口 ±5 分钟,``request_nonce``
   表 UNIQUE(subject, nonce) 兜住并发重放,TTL 清理。生产缝:换 Redis SETNX+TTL。
6. **PubSub**:``PubSubBroker`` ABC + ``InMemoryBroker``(asyncio 队列;从同步路由
   发布时用 ``loop.call_soon_threadsafe``)。生产缝:换 Redis pub/sub。

⚠️ MOCK 边界:未配置 ``JWT_SECRET`` 时使用开发缺省 secret 并打 MOCK 级警告日志;
真实部署必须注入环境变量。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Iterable, Mapping

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import AppError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. 配置(env 可覆盖;缺省值仅供本机开发)
# ---------------------------------------------------------------------------

AUTH_MODE_ENV = "AUTH_MODE"
#: 生产/默认:所有 ``/api/c``、``/api/b`` 必须带角色正确的 JWT。
AUTH_MODE_ENFORCE = "enforce"
#: 内部调用链(回归脚本 check_w1_cases.py / demo_w2.py 用 TestClient 直连,不带 token)
#: 专用:跳过 HTTP 鉴权,身份由路由从请求体内部兜底并打 WARN。**不得用于对外服务。**
AUTH_MODE_INTERNAL = "internal"

JWT_ALG = "HS256"
JWT_TTL_SECONDS = 2 * 3600
#: 开发缺省 secret(MOCK):只在本机无 env 时使用,生产必须注入。
DEV_JWT_SECRET = "dev-w8-insecure-jwt-secret-change-me"
_JWT_SECRET_WARNED = False

PASSWORD_ITERATIONS = 200_000
PASSWORD_SALT_BYTES = 16

#: 限流/防重放开关:由测试在 conftest 里关闭(既有 434 测试是毫秒级连发/不带重放头),
#: 新测试再显式打开。默认开。
RATE_LIMIT_ENABLED = True
REPLAY_GUARD_ENABLED = True

USER_RATE_PER_SECOND = 5.0
USER_BURST = 10
TENANT_RATE_PER_SECOND = 50.0
TENANT_BURST = 50

REPLAY_WINDOW_SECONDS = 300
REPLAY_NONCE_MIN_LEN = 8
REPLAY_NONCE_MAX_LEN = 128
#: 日 token 配额(chat 轮次;tokens_in + tokens_out 聚合)。
DAILY_TOKEN_QUOTA = 200_000

CORS_ORIGINS_ENV = "CORS_ORIGINS"
#: 本机三端口(127.0.0.1:8000 / localhost:8000 / 127.0.0.1:8033)。**不允许 ``*``**。
DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:8033"
)


def auth_mode() -> str:
    """当前鉴权模式:``enforce``(默认)或 ``internal``(仅内部回归脚本)。"""
    value = (os.environ.get(AUTH_MODE_ENV) or AUTH_MODE_ENFORCE).strip().lower()
    return AUTH_MODE_INTERNAL if value == AUTH_MODE_INTERNAL else AUTH_MODE_ENFORCE


def jwt_secret() -> bytes:
    """JWT 签名 secret:优先 env ``JWT_SECRET``;缺省开发值 + MOCK 警告(只警告一次)。"""
    global _JWT_SECRET_WARNED
    secret = os.environ.get("JWT_SECRET")
    if secret:
        return secret.encode("utf-8")
    if not _JWT_SECRET_WARNED:
        _JWT_SECRET_WARNED = True
        logger.warning(
            "JWT_SECRET 未配置,使用开发缺省 secret(MOCK 级);生产部署必须注入 JWT_SECRET"
        )
    return DEV_JWT_SECRET.encode("utf-8")


def cors_origins() -> list[str]:
    """CORS 白名单(env ``CORS_ORIGINS`` 逗号分隔;任何 ``*`` 都被剔除)。"""
    raw = os.environ.get(CORS_ORIGINS_ENV) or DEFAULT_CORS_ORIGINS
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    return [item for item in origins if item != "*"]


# ---------------------------------------------------------------------------
# 2. 密码哈希(pbkdf2_hmac-sha256,标准库)
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    """生成 ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`` 口令哈希。

    只存哈希、不存明文;每次新盐。
    """
    salt = secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, _b64e(salt), _b64e(digest)
    )


def verify_password(password: str, stored: str) -> bool:
    """常量时间比对口令哈希;格式非法/算法不符一律 False(不抛异常)。"""
    try:
        algo, iterations_raw, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, AttributeError):
        return False
    if iterations <= 0:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# 3. JWT(HS256 手写:base64url(header).base64url(payload).HMAC-SHA256)
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """token 校验失败。``code`` 为对外统一错误码(AUTH_INVALID / AUTH_EXPIRED)。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Identity:
    """请求身份(只由 token 或内部模式下显式兜底构造,路由不得自行拼)。"""

    sub: str
    role: str  # buyer | agent | internal
    merchant_id: str
    customer_id: str | None = None
    service_id: str | None = None

    @property
    def is_internal(self) -> bool:
        return self.role == "internal"

    def to_claims(self, *, now: datetime | None = None, ttl_seconds: int = JWT_TTL_SECONDS) -> dict:
        issued = now or utc_now()
        claims: dict[str, Any] = {
            "sub": self.sub,
            "role": self.role,
            "merchant_id": self.merchant_id,
            "iat": int(issued.timestamp()),
            "exp": int((issued + timedelta(seconds=ttl_seconds)).timestamp()),
        }
        if self.customer_id:
            claims["customer_id"] = self.customer_id
        if self.service_id:
            claims["service_id"] = self.service_id
        return claims


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(signing_input: bytes) -> str:
    return _b64e(hmac.new(jwt_secret(), signing_input, hashlib.sha256).digest())


def create_token(
    identity: Identity,
    *,
    ttl_seconds: int = JWT_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    """签发 HS256 JWT(默认 2 小时)。payload: sub/role/merchant_id/customer_id|service_id/iat/exp。"""
    header = _b64e(
        json.dumps({"alg": JWT_ALG, "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
    )
    payload = _b64e(
        json.dumps(
            identity.to_claims(now=now, ttl_seconds=ttl_seconds),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{_sign(signing_input)}"


def decode_token(token: str, *, now: datetime | None = None) -> Identity:
    """校验并解析 HS256 JWT;签名篡改/格式错 → AUTH_INVALID,过期 → AUTH_EXPIRED。"""
    if not token or token.count(".") != 2:
        raise TokenError("AUTH_INVALID", "token 格式错误")
    header_b64, payload_b64, signature = token.split(".")
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    if not hmac.compare_digest(_sign(signing_input), signature):
        raise TokenError("AUTH_INVALID", "token 签名无效")
    try:
        header = json.loads(_b64d(header_b64).decode("utf-8"))
        claims = json.loads(_b64d(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise TokenError("AUTH_INVALID", "token 载荷无法解析") from None
    if header.get("alg") != JWT_ALG:
        raise TokenError("AUTH_INVALID", "token 算法不受支持")
    role = str(claims.get("role") or "")
    if role not in ("buyer", "agent"):
        raise TokenError("AUTH_INVALID", "token 角色非法")
    try:
        expires_at = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        raise TokenError("AUTH_INVALID", "token 过期时间非法") from None
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expires_at <= int(current.timestamp()):
        raise TokenError("AUTH_EXPIRED", "token 已过期")
    sub = str(claims.get("sub") or "")
    merchant_id = str(claims.get("merchant_id") or "")
    if not sub or not merchant_id:
        raise TokenError("AUTH_INVALID", "token 缺少主体或租户")
    customer_id = claims.get("customer_id")
    service_id = claims.get("service_id")
    return Identity(
        sub=sub,
        role=role,
        merchant_id=merchant_id,
        customer_id=str(customer_id) if customer_id else None,
        service_id=str(service_id) if service_id else None,
    )


# ---------------------------------------------------------------------------
# 4. 身份依赖(路由统一从这里拿身份;禁止自行从请求体拼)
# ---------------------------------------------------------------------------


def current_identity(request: Request) -> Identity:
    """FastAPI 依赖:读取鉴权中间件写入 ``request.state.identity`` 的身份。"""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise AppError(
            code="AUTH_REQUIRED",
            message="缺少身份信息(请先登录)",
            status_code=401,
        )
    return identity


def effective_identity(
    identity: Identity,
    role: str,
    *,
    payload: Any | None = None,
    fallback: Mapping[str, Any] | None = None,
) -> Identity:
    """**唯一**的内部兜底入口(``AUTH_MODE=internal`` 专用,enforce 模式恒等返回)。

    check_w1_cases.py / demo_w2.py 用 TestClient 直连、不带 token,按任务书回归红线
    不能改它们,因此在 internal 模式下允许从请求体(payload)或 query 参数
    (``fallback``)取身份,并打 WARN。对外 enforce 模式下这里直接返回 token 身份,
    请求体/query 里的 customer_id/service_id **没有任何身份效力**
    (测试 ``test_auth.py::test_body_customer_id_has_no_effect``)。
    """
    if not identity.is_internal:
        return identity

    def _pick(name: str) -> Any:
        value = getattr(payload, name, None)
        if value is None and fallback is not None:
            value = fallback.get(name)
        return value

    customer_id = _pick("customer_id")
    service_id = _pick("service_id")
    merchant_id = _pick("merchant_id") or identity.merchant_id
    subject = customer_id or service_id or identity.sub
    logger.warning(
        "AUTH_MODE=internal:身份由请求体内部兜底(仅回归脚本),role=%s sub=%s",
        role,
        subject,
    )
    return Identity(
        sub=str(subject),
        role=role,
        merchant_id=str(merchant_id),
        customer_id=str(customer_id) if customer_id else None,
        service_id=str(service_id) if service_id else None,
    )


# ---------------------------------------------------------------------------
# 5. 限流:RateLimiter ABC + 内存令牌桶
# ---------------------------------------------------------------------------


class RateLimiter(ABC):
    """限流器接口(生产缝:Redis INCR + EXPIRE 实现同签名)。"""

    @abstractmethod
    def consume(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """消费一次配额;返回 ``(是否放行, 拒绝时建议的 Retry-After 秒数)``。"""


class MemoryRateLimiter(RateLimiter):
    """进程内令牌桶:每秒补充 ``rate`` 个令牌,桶容量 ``burst``。"""

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self.rate = float(rate_per_second)
        self.burst = int(burst)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def consume(self, key: str, now: float | None = None) -> tuple[bool, float]:
        current = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), current))
            tokens = min(float(self.burst), tokens + max(0.0, current - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, current)
                return True, 0.0
            self._buckets[key] = (tokens, current)
            retry_after = (1.0 - tokens) / self.rate if self.rate > 0 else 1.0
            return False, max(retry_after, 0.001)


_user_limiter = MemoryRateLimiter(USER_RATE_PER_SECOND, USER_BURST)
_tenant_limiter = MemoryRateLimiter(TENANT_RATE_PER_SECOND, TENANT_BURST)


def check_rate_limit(
    identity: Identity, *, now: float | None = None
) -> float | None:
    """用户级 + 租户级双重令牌桶;放行返回 None,拒绝返回 Retry-After 秒数。"""
    if not RATE_LIMIT_ENABLED:
        return None
    allowed, retry_after = _user_limiter.consume(identity.sub, now)
    if not allowed:
        return retry_after
    allowed, retry_after = _tenant_limiter.consume(identity.merchant_id, now)
    if not allowed:
        return retry_after
    return None


# --- chat 日 token 配额(按 token_usage 表聚合) -----------------------------


def _utc_day(now: datetime | None = None) -> str:
    current = now or utc_now()
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc)
    return current.date().isoformat()


def used_tokens_today(db: Session, subject: str, *, now: datetime | None = None) -> int:
    """该主体今日已用 token(SUM(tokens_in + tokens_out))。"""
    from app.models import TokenUsage

    total = db.scalar(
        select(
            func.coalesce(func.sum(TokenUsage.tokens_in + TokenUsage.tokens_out), 0)
        ).where(TokenUsage.day == _utc_day(now), TokenUsage.subject == subject)
    )
    return int(total or 0)


def enforce_token_quota(
    db: Session, subject: str, *, now: datetime | None = None
) -> None:
    """日 token 配额检查;超限抛 429 ``TOKEN_QUOTA_EXCEEDED``(不写库)。"""
    if used_tokens_today(db, subject, now=now) >= DAILY_TOKEN_QUOTA:
        raise AppError(
            code="TOKEN_QUOTA_EXCEEDED",
            message=f"当前主体今日 token 配额已用尽(上限 {DAILY_TOKEN_QUOTA})",
            status_code=429,
            retryable=True,
            param="tokens",
        )


def record_token_usage(
    db: Session,
    subject: str,
    tokens_in: int,
    tokens_out: int,
    *,
    now: datetime | None = None,
) -> None:
    """累加今日用量(upsert;调用方负责 commit)。"""
    from app.models import TokenUsage

    day = _utc_day(now)
    row = db.scalar(
        select(TokenUsage).where(TokenUsage.day == day, TokenUsage.subject == subject)
    )
    if row is None:
        db.add(
            TokenUsage(
                subject=subject,
                day=day,
                tokens_in=max(0, int(tokens_in)),
                tokens_out=max(0, int(tokens_out)),
            )
        )
    else:
        row.tokens_in = int(row.tokens_in) + max(0, int(tokens_in))
        row.tokens_out = int(row.tokens_out) + max(0, int(tokens_out))


# ---------------------------------------------------------------------------
# 6. 防重放:X-Timestamp + X-Nonce(窗口 ±5 分钟 + nonce 表 UNIQUE)
# ---------------------------------------------------------------------------


def _replay_error(status_code: int, code: str, message: str) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)


def require_replay_guard(
    request: Request,
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_nonce: str | None = Header(default=None, alias="X-Nonce"),
    db: Session = Depends(get_db),
) -> None:
    """写接口防重放依赖(confirm/reject/claim/close/reply 五个写接口挂载)。

    - 缺头 → 400 ``REPLAY_HEADER_REQUIRED``
    - 时间戳非整数 / 超出 ±5 分钟窗口 → 401 ``REPLAY_TIMESTAMP_INVALID``
    - 同 (subject, nonce) 二次出现 → 409 ``REPLAY_DETECTED``(DB UNIQUE 兜并发)
    - nonce 长度非法 → 400
    """
    if not REPLAY_GUARD_ENABLED:
        return
    from app.models import RequestNonce

    if not x_timestamp or not x_nonce:
        raise _replay_error(
            400, "REPLAY_HEADER_REQUIRED", "写接口必须携带 X-Timestamp 与 X-Nonce 头"
        )
    if not (REPLAY_NONCE_MIN_LEN <= len(x_nonce) <= REPLAY_NONCE_MAX_LEN):
        raise _replay_error(400, "REPLAY_NONCE_INVALID", "X-Nonce 长度非法")
    try:
        timestamp = int(str(x_timestamp).strip())
    except (TypeError, ValueError):
        raise _replay_error(401, "REPLAY_TIMESTAMP_INVALID", "X-Timestamp 必须是 Unix 秒整数") from None
    now = utc_now()
    now_ts = int(now.timestamp())
    if abs(now_ts - timestamp) > REPLAY_WINDOW_SECONDS:
        raise _replay_error(
            401, "REPLAY_TIMESTAMP_INVALID", "X-Timestamp 超出允许的时间窗口(±5 分钟)"
        )

    identity = getattr(request.state, "identity", None)
    subject = identity.sub if identity is not None else "anonymous"

    # TTL 清理:窗口之外的老 nonce 行每次写入时顺手清掉(本机 SQLite,成本可忽略)。
    cutoff = now - timedelta(seconds=REPLAY_WINDOW_SECONDS * 2)
    db.execute(delete(RequestNonce).where(RequestNonce.created_at < cutoff))
    db.flush()

    db.add(RequestNonce(subject=subject, nonce=x_nonce, ts=timestamp))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _replay_error(409, "REPLAY_DETECTED", "该 nonce 已被使用(疑似重放请求)") from None


# ---------------------------------------------------------------------------
# 7. PubSub:PubSubBroker ABC + asyncio 内存实现
# ---------------------------------------------------------------------------


class PubSubBroker(ABC):
    """发布/订阅接口(生产缝:Redis pub/sub 或 MQ;WS 端点只依赖本接口)。"""

    @abstractmethod
    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        """向频道广播一条事件。"""

    @abstractmethod
    def subscribe(self, channels: Iterable[str]) -> Any:
        """异步上下文管理器:``async with broker.subscribe([ch]) as queue``。"""


class _Subscription:
    __slots__ = ("queue", "loop")

    def __init__(self, queue: "asyncio.Queue[dict[str, Any]]", loop: asyncio.AbstractEventLoop) -> None:
        self.queue = queue
        self.loop = loop


def _enqueue(queue: "asyncio.Queue[dict[str, Any]]", event: dict[str, Any]) -> None:
    if not queue.full():
        queue.put_nowait(event)


class InMemoryBroker(PubSubBroker):
    """进程内 asyncio 队列广播(单进程本机形态;多 worker 需换 Redis)。"""

    def __init__(self) -> None:
        self._subscriptions: dict[str, list[_Subscription]] = {}
        self._lock = threading.Lock()

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        self.publish_nowait(channel, event)

    def publish_nowait(self, channel: str, event: dict[str, Any]) -> None:
        """同步发布入口(同步路由用):跨线程安全地塞进各订阅者队列。"""
        with self._lock:
            subscriptions = list(self._subscriptions.get(channel, ()))
        for subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(
                    _enqueue, subscription.queue, dict(event)
                )
            except RuntimeError:  # pragma: no cover - 事件循环已关闭
                continue

    @asynccontextmanager
    async def subscribe(self, channels: Iterable[str]) -> AsyncIterator["asyncio.Queue[dict[str, Any]]"]:
        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue(maxsize=256)
        names = [name for name in channels if name]
        subscription = _Subscription(queue, loop)
        with self._lock:
            for name in names:
                self._subscriptions.setdefault(name, []).append(subscription)
        try:
            yield queue
        finally:
            with self._lock:
                for name in names:
                    listeners = self._subscriptions.get(name, [])
                    if subscription in listeners:
                        listeners.remove(subscription)
                    if not listeners:
                        self._subscriptions.pop(name, None)


BROKER: PubSubBroker = InMemoryBroker()


def channel_buyer(session_id: str) -> str:
    return f"buyer:{session_id}"


def channel_agent(service_id: str) -> str:
    return f"agent:{service_id}"


def channel_queue(merchant_id: str) -> str:
    return f"queue:{merchant_id}"


def publish_event(channel: str, event_type: str, **payload: Any) -> None:
    """同步路由写库后广播事件(事件体统一带 ``type`` 与 UTC ``ts``)。"""
    event: dict[str, Any] = {
        "type": event_type,
        "ts": utc_now().isoformat(),
    }
    event.update(payload)
    publisher = BROKER
    if isinstance(publisher, InMemoryBroker):
        publisher.publish_nowait(channel, event)
    else:  # pragma: no cover - 未来 Redis broker 由 event loop 调度
        try:
            asyncio.get_running_loop().create_task(publisher.publish(channel, event))
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# 8. 鉴权中间件用的路径/头解析 + 测试用状态重置
# ---------------------------------------------------------------------------

#: 完全放行的路径(登录、健康检查、指标、文档、静态页;WS 在端点内自行鉴权)。
PUBLIC_PATHS = (
    "/",
    "/health",
    "/metrics",
    "/favicon.ico",
    "/favicon.svg",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/auth/login",
)
PUBLIC_PREFIXES = ("/ws/",)
#: 只需要"任意合法身份"的路径(如 /auth/me:登录态校验,不区分角色)。
ANY_ROLE_PATHS = ("/auth/me",)
#: 角色路由前缀:``/api/c/*`` 只认 buyer,``/api/b/*`` 只认 agent。
ROLE_PREFIXES: tuple[tuple[str, str], ...] = (("/api/c/", "buyer"), ("/api/b/", "agent"))

#: ``required_role`` 的特殊返回值:要求合法 token 但不校验角色。
ROLE_ANY = "any"


def required_role(path: str) -> str | None:
    """该路径需要的角色;None = 放行(静态页/健康检查/登录)。"""
    if path in PUBLIC_PATHS:
        return None
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return None
    if path in ANY_ROLE_PATHS:
        return ROLE_ANY
    for prefix, role in ROLE_PREFIXES:
        if path.startswith(prefix):
            return role
    return None


def bearer_token(header_value: str | None) -> str | None:
    """从 ``Authorization: Bearer xxx`` 解析 token。"""
    if not header_value:
        return None
    parts = header_value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def error_response(status_code: int, code: str, message: str, *, retryable: bool = False, param: Any = None) -> JSONResponse:
    """中间件用的统一错误响应体(与 ``app.errors._body`` 同形状,避免循环导入)。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "param": param,
            },
        },
    )


def reset_security_state() -> None:
    """重置进程内安全状态(限流桶 / 订阅表);测试 fixture 与 logout 场景用。"""
    global _user_limiter, _tenant_limiter, BROKER
    _user_limiter = MemoryRateLimiter(USER_RATE_PER_SECOND, USER_BURST)
    _tenant_limiter = MemoryRateLimiter(TENANT_RATE_PER_SECOND, TENANT_BURST)
    BROKER = InMemoryBroker()
