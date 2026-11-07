"""工具注册表:九字段契约 + 三层校验(schema → resolve → 业务规则)+ 统一调用入口。

九字段(任务书 W2-B + 03§4.2.3):
``name / description / input_schema / output_schema / side_effect /
timeout_s / retry / idempotency_key / requires_confirm``。

三层校验分工:
- ``registry.invoke`` 只做 **schema 层**:用 pydantic v2 ``TypeAdapter`` 校验入参,
  失败回喂 ``SCHEMA_ERROR``(不重试)。
- 各工具 ``handler`` 做 **resolve 层**(资源存在 + 归属)与 **业务规则层**,
  失败抛 ``ToolExecutionError``(NOT_FOUND_OR_FORBIDDEN / BUSINESS_REJECT 等)。
- 写工具(``requires_confirm=True``)的 ``invoke`` 不真正落业务数据,只生成
  确认卡 + 落 ``confirm_request``;真正执行在 W3 ``invoke_confirmed``。

失败四分类(PRD §4):SCHEMA_ERROR / NOT_FOUND_OR_FORBIDDEN / BUSINESS_REJECT /
UNKNOWN_OUTCOME(超时,retryable:false,先查幂等台账)/ TRANSIENT(≤2 退避)。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.tools.events import ToolEvent, create_confirm, write_event
from app.tools.results import (
    CODE_BUSINESS_REJECT,
    CODE_NOT_FOUND_OR_FORBIDDEN,
    CODE_SCHEMA_ERROR,
    CODE_UNKNOWN_OUTCOME,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)

logger = logging.getLogger("app.tools.registry")


@dataclass(frozen=True)
class ToolSpec:
    """工具九字段契约(frozen,注册后不可变)。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect: str  # "read" | "write" | "external" | "none"
    timeout_s: float
    retry: dict[str, Any]  # {"max_attempts": n, "backoff_s": s}
    idempotency_key: list[str] | None  # 组成幂等键的入参字段名;None = 无幂等键
    requires_confirm: bool

    def to_dict(self) -> dict[str, Any]:
        """九字段顺序固定序列化(验收 dump 用)。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "side_effect": self.side_effect,
            "timeout_s": self.timeout_s,
            "retry": self.retry,
            "idempotency_key": self.idempotency_key,
            "requires_confirm": self.requires_confirm,
        }


@dataclass
class _Entry:
    spec: ToolSpec
    adapter: TypeAdapter[Any] = field(repr=False)
    handler: Callable[[dict[str, Any], ToolContext], dict[str, Any]] = field(repr=False)


_REGISTRY: dict[str, _Entry] = {}


def _strip_title(obj: Any) -> Any:
    """递归去掉 JSON Schema 里 pydantic 自动生成的 ``title``(驼峰,非契约噪音)。"""
    if isinstance(obj, dict):
        return {k: _strip_title(v) for k, v in obj.items() if k != "title"}
    if isinstance(obj, list):
        return [_strip_title(v) for v in obj]
    return obj


def clean_schema(model: type[BaseModel]) -> dict[str, Any]:
    """从 pydantic v2 输入模型派生 input_schema(等价 JSON Schema,去 title 噪音)。

    契约保证:``additionalProperties:false``(extra='forbid')、``required`` 显式、
    ``pattern``/``enum``/``minimum`` 由 Field/Literal 派生。
    """
    return _strip_title(model.model_json_schema())


def register(
    spec: ToolSpec,
    input_model: type[BaseModel],
    handler: Callable[[dict[str, Any], ToolContext], dict[str, Any]],
) -> None:
    """注册一个工具(九字段 + pydantic 输入模型 + handler)。"""
    _REGISTRY[spec.name] = _Entry(spec=spec, adapter=TypeAdapter(input_model), handler=handler)


def get_spec(name: str) -> ToolSpec | None:
    """按名取九字段;未知工具返回 None。"""
    entry = _REGISTRY.get(name)
    return entry.spec if entry is not None else None


def list_specs() -> list[ToolSpec]:
    """按名排序返回全部已注册工具九字段。"""
    return [e.spec for e in sorted(_REGISTRY.values(), key=lambda e: e.spec.name)]


def dump_specs() -> list[dict[str, Any]]:
    """九字段总表 dump(验收用)。"""
    return [spec.to_dict() for spec in list_specs()]


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _next_step(session: Session, trace_id: str) -> int:
    """同一 trace_id 下自增 step(max(step)+1,首条为 0)。"""
    max_step = session.scalar(
        select(func.max(ToolEvent.step)).where(ToolEvent.trace_id == trace_id)
    )
    return 0 if max_step is None else int(max_step) + 1


def _write_event(
    ctx: ToolContext,
    *,
    tool_name: str,
    ok: bool,
    error_code: str | None,
    payload: Any,
) -> ToolEvent:
    """写一条 append-only 工具调用事件(调用方负责 commit)。"""
    return write_event(
        ctx.session,
        trace_id=ctx.trace_id,
        step=_next_step(ctx.session, ctx.trace_id),
        tool_name=tool_name,
        ok=ok,
        error_code=error_code,
        payload=payload,
    )


def _invoke_impl(name: str, args: Any, ctx: ToolContext) -> ToolResult:
    """同步执行一条工具调用(无超时;超时在 ainvoke 的 asyncio.wait_for 层)。"""
    start = time.perf_counter()
    entry = _REGISTRY.get(name)
    if entry is None:
        return ToolResult.failure(
            CODE_NOT_FOUND_OR_FORBIDDEN,
            f"未知工具:{name}",
            tool=name,
            duration_ms=_elapsed_ms(start),
            retryable=False,
        )
    spec = entry.spec

    # 1) schema 层:TypeAdapter 校验,失败回喂 SCHEMA_ERROR(不重试)
    try:
        validated = entry.adapter.validate_python(args)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = first.get("loc", ())
        param = ".".join(str(x) for x in loc) if loc else None
        message = f"参数校验失败:{param}({first.get('msg', 'invalid')})"
        _write_event(
            ctx, tool_name=name, ok=False, error_code=CODE_SCHEMA_ERROR,
            payload={"args": args},
        )
        ctx.session.commit()
        return ToolResult.failure(
            CODE_SCHEMA_ERROR,
            message,
            tool=name,
            duration_ms=_elapsed_ms(start),
            retryable=False,
            param=param,
        )

    args_dict: dict[str, Any] = (
        validated.model_dump() if isinstance(validated, BaseModel) else validated
    )

    # 2) resolve + 业务规则层:handler 内抛 ToolExecutionError / 其他异常
    try:
        data = entry.handler(args_dict, ctx)
    except ToolExecutionError as exc:
        _write_event(
            ctx, tool_name=name, ok=False, error_code=exc.code,
            payload={"args": args_dict},
        )
        ctx.session.commit()
        return ToolResult.failure(
            exc.code,
            exc.message,
            tool=name,
            duration_ms=_elapsed_ms(start),
            retryable=exc.retryable,
            param=exc.param,
        )
    except Exception as exc:  # noqa: BLE001 —— 未捕获异常兜底为 UNKNOWN_OUTCOME
        logger.exception("工具 %s 未捕获异常", name)
        _write_event(
            ctx, tool_name=name, ok=False, error_code=CODE_UNKNOWN_OUTCOME,
            payload={"args": args_dict},
        )
        ctx.session.commit()
        return ToolResult.failure(
            CODE_UNKNOWN_OUTCOME,
            f"工具执行异常,结果未知:{exc}",
            tool=name,
            duration_ms=_elapsed_ms(start),
            retryable=False,
        )

    # 3) 写工具 requires_confirm=True:不落业务数据,只生成确认卡 + token 落库
    if spec.requires_confirm:
        card = data["card"]
        confirm = create_confirm(
            ctx.session,
            tool_name=name,
            args=data.get("args", args_dict),
            card=card,
        )
        _write_event(
            ctx, tool_name=name, ok=True, error_code=None,
            payload={"args": args_dict, "pending_confirm": True},
        )
        ctx.session.commit()
        return ToolResult.success(
            {
                "pending_confirm": {
                    "what": card["what"],
                    "impact": card["impact"],
                    "irreversibility": card["irreversibility"],
                    "alternatives": card["alternatives"],
                    "confirm_token": confirm.confirm_token,
                }
            },
            tool=name,
            duration_ms=_elapsed_ms(start),
        )

    # 4) 读工具:直接返回 handler 结果
    _write_event(
        ctx, tool_name=name, ok=True, error_code=None, payload={"args": args_dict},
    )
    ctx.session.commit()
    return ToolResult.success(data, tool=name, duration_ms=_elapsed_ms(start))


def invoke(name: str, args: Any, ctx: ToolContext) -> ToolResult:
    """统一调用入口(同步)。测试/同步编排用;生产异步编排用 ``ainvoke``。"""
    return _invoke_impl(name, args, ctx)


async def ainvoke(name: str, args: Any, ctx: ToolContext) -> ToolResult:
    """统一调用入口(异步):用注册表 ``timeout_s`` 包 ``asyncio.wait_for``。

    超时 → ``UNKNOWN_OUTCOME``(retryable:false,先查幂等台账)。handler 是纯只读
    校验(写工具的真实落库在 ``execute``,W3 用),超时后孤儿线程只跑无副作用的
    handler,不会写出脏事件。
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        return _invoke_impl(name, args, ctx)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_invoke_impl, name, args, ctx),
            timeout=entry.spec.timeout_s,
        )
    except asyncio.TimeoutError:
        return ToolResult.failure(
            CODE_UNKNOWN_OUTCOME,
            "工具执行超时,结果未知,请先查幂等台账确认是否已生效",
            tool=name,
            duration_ms=entry.spec.timeout_s * 1000.0,
            retryable=False,
        )
