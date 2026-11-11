"""trace.py — trace_id 生成与结构化 trace 事件(W2-C 新增)。

设计要点:
- 一次"进线→出口"的整条链路共用一个 trace_id:由 API 入口生成,随响应返回,
  并写入每条 chat_message 与会话链路上的所有事件;
- 工具调用事件由 ``app.tools.registry.invoke`` 自动写入 tool_events(type='tool_call');
- 本模块负责补写编排层事件:意图判定、会话状态变更、入口拦截。它们同样落
  tool_events 表,用 ``tool_name`` 命名空间 + ``payload["event"]`` 区分事件类别。

为什么不新增 event 类型:tool_events.type 的 CHECK 约束 ``type = 'tool_call'``(迁移
0002)属于已验收冻结的 W2-B 产物,本任务书白名单不含新迁移,故沿用既有 type,
以命名空间区分事件类别。W3-A 上图时如需独立 event_type 列,随图迁移一并处理。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.tools.events import ToolEvent, write_event

logger = logging.getLogger("app.trace")

# ---------------------------------------------------------------------------
# 事件名(命名空间约定:模块__动作)
# ---------------------------------------------------------------------------

EVENT_CLASSIFY = "router__classify"          # 意图判定(含快捷路径 / FakeLLM 路径)
EVENT_SESSION_STATUS = "session__status_change"  # 会话状态变更(active->handed_off 等)
EVENT_ROUTE_BLOCKED = "route__blocked"       # 入口拦截:会话非 active,AI 停止回复
EVENT_QUERY_REWRITE = "retrieval__query_rewrite"  # 无命中时的确定性查询改写(MOCK)
#: W15 U7:辅助模块(语义缓存读取 / 建议条款检索 / SLA 扫描)失败时的降级事件。
#: 语义是"辅助路径挂了但主链路照常走完",不是会话失败。
EVENT_AUX_DEGRADED = "aux_degraded"


def new_trace_id() -> str:
    """生成 trace_id:优先 uuid7(时间有序),运行环境无 uuid7 时退回 uuid4。"""
    uuid7 = getattr(uuid, "uuid7", None)
    if callable(uuid7):  # Python 3.14+ / 部分 3.13 环境
        try:
            return str(uuid7())
        except Exception:  # pragma: no cover - 不同实现的兜底
            pass
    return uuid.uuid4().hex


def next_step(session: Session, trace_id: str) -> int:
    """同一 trace_id 内的下一个 step(单调递增,从 0 开始)。"""
    current = session.scalar(
        select(func.max(ToolEvent.step)).where(ToolEvent.trace_id == trace_id)
    )
    return 0 if current is None else int(current) + 1


def record_event(
    session: Session,
    *,
    trace_id: str,
    event: str,
    payload: dict[str, Any] | None = None,
    ok: bool = True,
    error_code: str | None = None,
) -> ToolEvent:
    """写一条编排层 trace 事件(复用 tool_events 表,命名空间区分)。"""
    body: dict[str, Any] = {"event": event}
    if payload:
        body.update(payload)
    tool_event = write_event(
        session,
        trace_id=trace_id,
        step=next_step(session, trace_id),
        tool_name=event,
        ok=ok,
        error_code=error_code,
        payload=body,
    )
    # 立即 flush:registry 的 _next_step 走数据库 max(step),部分 Session 配置了
    # autoflush=False,不 flush 会导致同 trace 内 step 撞号(演示脚本即真实库 SessionLocal)。
    session.flush()
    return tool_event


def record_aux_degraded(
    session: Session,
    *,
    trace_id: str,
    source: str,
    error: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """W15 U7:辅助模块降级事件(语义缓存/建议检索/SLA 扫描等)。

    约定:辅助路径失败**不得**中断主链路,只落一条 ``aux_degraded`` 事件,
    事件写入本身也吞异常(降级记录失败不能反过来把主链路打断)。
    """
    body: dict[str, Any] = {"source": source}
    if error:
        body["error"] = error[:400]
    if payload:
        body.update(payload)
    try:
        record_event(
            session,
            trace_id=trace_id,
            event=EVENT_AUX_DEGRADED,
            payload=body,
            ok=False,
            error_code="aux_degraded",
        )
    except Exception:  # pragma: no cover - 降级事件写失败也不能阻断主链路
        logger.exception("aux_degraded 事件写入失败: source=%s", source)


def trace_events(session: Session, trace_id: str) -> list[ToolEvent]:
    """按 step 升序取回一条链路的全部事件(演示/测试用)。"""
    return list(
        session.scalars(
            select(ToolEvent)
            .where(ToolEvent.trace_id == trace_id)
            .order_by(ToolEvent.step)
        )
    )
