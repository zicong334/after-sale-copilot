"""ToolResult / ToolError 五层契约回包模型 + 失败四分类错误码常量。

统一回包(三件套,PRD §5 + 03§4.2.3):
- 成功:``{"ok": True, "data": {...}, "meta": {"tool", "duration_ms", "cached"}}``
- 失败:``{"ok": False, "error": {"code", "message", "retryable", "param"}, "meta": {...}}``

失败四分类(PRD §4 失败语义 + 任务书 W2-B 实现要点 3):
- ``SCHEMA_ERROR``         参数格式错 → 回喂模型自检,不重试
- ``NOT_FOUND_OR_FORBIDDEN`` 资源不存在/越权 → 不重试(对外不区分,内部日志记真因)
- ``BUSINESS_REJECT``      业务规则拒绝(如金额>实付)→ 不重试
- ``TIMEOUT`` 场景 → ``UNKNOWN_OUTCOME``:可能已成功,retryable:false,先查幂等台账
- ``TRANSIENT``            明确的瞬时错误 → 有限重试(≤2)、退避、复用同一幂等键
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# 失败四分类错误码(五类 code;TIMEOUT 场景统一映射到 UNKNOWN_OUTCOME)
# ---------------------------------------------------------------------------
CODE_SCHEMA_ERROR = "SCHEMA_ERROR"
CODE_NOT_FOUND_OR_FORBIDDEN = "NOT_FOUND_OR_FORBIDDEN"
CODE_BUSINESS_REJECT = "BUSINESS_REJECT"
CODE_UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"  # 超时(可能已成功)→ 先查幂等台账
CODE_TRANSIENT = "TRANSIENT"


@dataclass
class ToolContext:
    """工具调用上下文:会话 + 租户 + 当前客户 + trace_id。

    - ``session``:同步 SQLAlchemy Session(与 W1-B services 一致)。
    - ``merchant_id / customer_id``:租户与当前客户,resolve 层做归属校验用。
    - ``trace_id``:一次调用链的贯穿标识,registry 用它给 tool_events 自增 step。
    """

    session: Session
    merchant_id: str
    customer_id: str
    trace_id: str
    # 当前聊天会话 id(W5-FIX R1):handoff 建单时写入 ticket.session_id,
    # 让"会话 → 工单"精确归属,不再按客户取最近一张。
    session_id: str | None = None


@dataclass
class ToolError:
    """错误四要素:code / message / retryable / param(PRD §4 回喂四要素)。"""

    code: str
    message: str
    retryable: bool = False
    param: Any = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为四要素(顺序固定,param 为 None 也保留,保证字段齐全)。"""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "param": self.param,
        }


@dataclass
class ToolResult:
    """工具统一回包。成功带 data,失败带 error;meta 恒含 tool/duration_ms/cached。"""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: ToolError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        data: dict[str, Any],
        tool: str,
        duration_ms: float,
        cached: bool = False,
    ) -> "ToolResult":
        return cls(
            ok=True,
            data=data,
            meta={"tool": tool, "duration_ms": round(duration_ms, 3), "cached": cached},
        )

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        tool: str,
        duration_ms: float,
        retryable: bool = False,
        param: Any = None,
        cached: bool = False,
    ) -> "ToolResult":
        return cls(
            ok=False,
            error=ToolError(code=code, message=message, retryable=retryable, param=param),
            meta={"tool": tool, "duration_ms": round(duration_ms, 3), "cached": cached},
        )

    def to_dict(self) -> dict[str, Any]:
        """三件套序列化。失败时 error 恒为四要素;成功时无 error 字段。"""
        out: dict[str, Any] = {"ok": self.ok, "meta": self.meta}
        if self.ok:
            out["data"] = self.data
        else:
            out["error"] = (
                self.error.to_dict()
                if self.error is not None
                else {
                    "code": "INTERNAL_ERROR",
                    "message": "未知错误",
                    "retryable": True,
                    "param": None,
                }
            )
        return out


class ToolExecutionError(Exception):
    """工具内部抛出的业务错误,由 registry.invoke 捕获并转成 ToolResult.failure。

    code 用失败四分类之一;message 面向模型(回喂);retryable/param 供编排层分类。
    """

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        param: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.param = param
