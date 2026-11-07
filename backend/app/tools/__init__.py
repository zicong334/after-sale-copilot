"""工具层:九字段注册表 + 5 个工具 + ToolResult/Event 三件套。

导入本包即完成:① ``app.tools.events`` 里的 ToolEvent/ConfirmRequest 注册进
Base.metadata(否则 conftest 的 create_all 不建这两张表);② 5 个工具注册进
``app.tools.registry``。

命名空间 ``{server}__{tool}`` 分发见正例 ai_service.py:234-244;此处只暴露
``registry`` 的 invoke/ainvoke 统一入口与九字段 dump。
"""
from __future__ import annotations

from app.tools import events  # noqa: F401 —— 触发 ToolEvent/ConfirmRequest 注册进 Base.metadata
from app.tools import handoff, policy, refund, shop  # noqa: F401 —— 触发工具注册
from app.tools.events import (
    CONFIRM_TTL_MINUTES,
    ConfirmRequest,
    ToolEvent,
    create_confirm,
    generate_confirm_token,
    get_confirm,
    write_event,
)
from app.tools.registry import (
    ToolSpec,
    clean_schema,
    dump_specs,
    get_spec,
    invoke,
    list_specs,
    register,
)
from app.tools.results import (
    CODE_BUSINESS_REJECT,
    CODE_NOT_FOUND_OR_FORBIDDEN,
    CODE_SCHEMA_ERROR,
    CODE_TRANSIENT,
    CODE_UNKNOWN_OUTCOME,
    ToolContext,
    ToolError,
    ToolExecutionError,
    ToolResult,
)

__all__ = [
    # 注册表
    "ToolSpec",
    "clean_schema",
    "register",
    "get_spec",
    "list_specs",
    "dump_specs",
    "invoke",
    # 三件套
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolExecutionError",
    # 错误码
    "CODE_SCHEMA_ERROR",
    "CODE_NOT_FOUND_OR_FORBIDDEN",
    "CODE_BUSINESS_REJECT",
    "CODE_UNKNOWN_OUTCOME",
    "CODE_TRANSIENT",
    # 事件 / 确认卡
    "ToolEvent",
    "ConfirmRequest",
    "write_event",
    "create_confirm",
    "get_confirm",
    "generate_confirm_token",
    "CONFIRM_TTL_MINUTES",
]
