"""统一错误体 + 9 个异常处理器。

约定(PRD §4 失败语义 + 五层契约 ToolResult.error):
- 错误响应体恒为 ``{ok: false, error: {code, message, retryable, param}}``,失败必须 ok:false 冒泡
- 业务冲突(状态机守卫失败)= 409 STATE_CONFLICT,param 带当前真实状态
- 金额超实付 = 422 AMOUNT_EXCEEDS_PAID
- 未捕获异常 = 500 INTERNAL_ERROR,响应不泄露堆栈(仅写 stderr 日志)
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")


#: R16:可判为「存储忙,稍后可重试」的错误特征(小写子串匹配)。
#: SQLite 锁竞争 + PG 序列化失败/死锁。**刻意不含** schema 错与磁盘 I/O 错 ——
#: 那些不是"稍后重试就好",伪装成 503 会让调用方白重试并掩盖真 bug。
_STORAGE_BUSY_MARKERS: tuple[str, ...] = (
    "database is locked",
    "database table is locked",
    "database is busy",
    "could not serialize access",
    "deadlock detected",
)

#: 存储忙时建议的退避秒数。**起步值,需按 SLO 校准**(本轮无 SLO,不拍最终值)。
_STORAGE_BUSY_RETRY_AFTER = 1


def is_storage_busy(exc: BaseException) -> bool:
    """该异常是否属于「存储忙,稍后可重试」(R16,2026-11-21 压测发现)。

    只对 ``OperationalError`` 生效,且按错误文本匹配锁/序列化/死锁特征。
    与存储无关的调用方错误、schema 错、磁盘错一律 False。
    """
    if not isinstance(exc, OperationalError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _STORAGE_BUSY_MARKERS)


class AppError(Exception):
    """业务错误基类:统一携带 code/message/retryable/param/status_code。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        retryable: bool = False,
        param: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.param = param


class StateConflictError(AppError):
    """状态机守卫失败(非法迁移 / 并发冲突 / 关单后写)。409。

    ``param`` 可显式指定冲突参数名(如 ``uq_refund_active``);未指定时回退到
    ``current_status``(状态机守卫失败时带上当前真实状态)。
    """

    def __init__(
        self,
        message: str,
        current_status: str | None = None,
        param: Any = None,
    ) -> None:
        super().__init__(
            code="STATE_CONFLICT",
            message=message,
            status_code=409,
            retryable=False,
            param=current_status if param is None else param,
        )


class NotFoundError(AppError):
    """资源不存在。404。"""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="NOT_FOUND",
            message=message,
            status_code=404,
            retryable=False,
        )


class AmountExceedsPaidError(AppError):
    """退款金额超过订单实付,不建单。422。"""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="AMOUNT_EXCEEDS_PAID",
            message=message,
            status_code=422,
            retryable=False,
        )


def _body(
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    param: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """统一错误响应体。``headers`` 用于 503 的 Retry-After(R16)。"""
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
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    """注册 9 个异常处理器(顺序与优先级无关,Starlette 按异常 MRO 匹配最近的)。"""

    # 1. 状态机冲突 → 409 STATE_CONFLICT(带当前真实状态)
    @app.exception_handler(StateConflictError)
    async def handle_state_conflict(_req, exc: StateConflictError):
        return _body(409, exc.code, exc.message, exc.retryable, exc.param)

    # 2. 资源不存在 → 404 NOT_FOUND
    @app.exception_handler(NotFoundError)
    async def handle_not_found(_req, exc: NotFoundError):
        return _body(404, exc.code, exc.message, exc.retryable, exc.param)

    # 3. 金额超实付 → 422 AMOUNT_EXCEEDS_PAID
    @app.exception_handler(AmountExceedsPaidError)
    async def handle_amount_exceeds(_req, exc: AmountExceedsPaidError):
        return _body(422, exc.code, exc.message, exc.retryable, exc.param)

    # 4. 其他业务错误 → 用实例自带 status/code
    @app.exception_handler(AppError)
    async def handle_app_error(_req, exc: AppError):
        return _body(exc.status_code, exc.code, exc.message, exc.retryable, exc.param)

    # 5. 请求体/参数校验失败 → 400 VALIDATION_ERROR
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_req, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = f"参数校验失败: {first.get('msg', 'invalid')}"
        return _body(400, "VALIDATION_ERROR", msg, False, loc or None)

    # 6. 数据库约束兜底(唯一冲突/CHECK 冲突)→ 409 DB_CONSTRAINT
    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(_req, exc: IntegrityError):
        logger.warning("数据库约束冲突: %s", exc)
        return _body(409, "DB_CONSTRAINT", "数据库约束冲突", False, None)

    # 7. R16(2026-11-21 压测发现):存储锁竞争 → **503 STORAGE_BUSY + Retry-After**。
    #    压测在写并发 8 时抓到裸 500 `database is locked`(chat.py:_resolve_session)。
    #    500 会招致客户端与网关重试,在已饱和的写路径上放大流量;503+Retry-After 才是受控降级。
    #    ⚠️ 刻意**不做整请求自动重试**:reference 明写「非幂等操作禁止自动重试」,
    #    对话 POST 可能已有工具自行 commit,重放会双写。重试交给持幂等键的调用方。
    #    非锁类 OperationalError(schema 错/磁盘错)仍走 500,不伪装成"稍后重试"。
    @app.exception_handler(OperationalError)
    async def handle_operational_error(_req, exc: OperationalError):
        if is_storage_busy(exc):
            logger.warning("存储忙(锁竞争/序列化冲突),受控降级 503: %s", exc)
            return _body(
                503, "STORAGE_BUSY", "存储繁忙,请稍后重试", True, None,
                headers={"Retry-After": str(_STORAGE_BUSY_RETRY_AFTER)},
            )
        logger.exception("数据库操作失败(非锁竞争)")
        return _body(500, "INTERNAL_ERROR", "服务器内部错误", True, None)

    # 8. Starlette/FastAPI HTTPException → 透传为统一错误体
    @app.exception_handler(StarletteHTTPException)
    async def handle_starlette_http(_req, exc: StarletteHTTPException):
        return _body(
            exc.status_code,
            "HTTP_ERROR",
            str(exc.detail),
            exc.status_code >= 500,
            None,
        )

    # 9. 未捕获异常 → 500 INTERNAL_ERROR,不泄露堆栈
    @app.exception_handler(Exception)
    async def handle_uncaught(_req, exc: Exception):
        logger.exception("未捕获异常(详情仅记录日志,不返回给客户端)")
        return _body(500, "INTERNAL_ERROR", "服务器内部错误", True, None)
