"""统一错误体 + 8 个异常处理器。

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
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")


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
    """状态机守卫失败(非法迁移 / 并发冲突 / 关单后写)。409。"""

    def __init__(self, message: str, current_status: str | None = None) -> None:
        super().__init__(
            code="STATE_CONFLICT",
            message=message,
            status_code=409,
            retryable=False,
            param=current_status,
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
) -> JSONResponse:
    """统一错误响应体。"""
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


def register_error_handlers(app: FastAPI) -> None:
    """注册 8 个异常处理器(顺序与优先级无关,Starlette 按异常 MRO 匹配最近的)。"""

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

    # 7. Starlette/FastAPI HTTPException → 透传为统一错误体
    @app.exception_handler(StarletteHTTPException)
    async def handle_starlette_http(_req, exc: StarletteHTTPException):
        return _body(
            exc.status_code,
            "HTTP_ERROR",
            str(exc.detail),
            exc.status_code >= 500,
            None,
        )

    # 8. 未捕获异常 → 500 INTERNAL_ERROR,不泄露堆栈
    @app.exception_handler(Exception)
    async def handle_uncaught(_req, exc: Exception):
        logger.exception("未捕获异常(详情仅记录日志,不返回给客户端)")
        return _body(500, "INTERNAL_ERROR", "服务器内部错误", True, None)
