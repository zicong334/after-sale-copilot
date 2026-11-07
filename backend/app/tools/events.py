"""工具调用 trace 事件 + 确认卡落库(``tool_events`` / ``confirm_request`` 两表)。

三铁律对齐:
- 时间 UTC:``ts / created_at`` 用 ``DateTime(timezone=True)`` + ``utc_now`` 默认值。
- 金额:本层不存金额字段(退款金额只在 refund_request 表,本层仅存 payload 摘要)。
- ``tool_events`` 是 append-only:只有 insert,无 update/delete 路径。

确认卡四要素(做什么/影响面/不可逆性/替代方案)落 ``confirm_request``,超时 10 分钟
默认拒绝;真正执行在 W3 ``invoke_confirmed(confirm_token)``,本任务书只做到
"生成确认卡 + token 落库"。

表位置说明:任务书白名单把迁移写在 ``backend/app/tools/migrations_add_tool_events.py``,
但 alembic ``script_location=backend/alembic`` 只扫描 ``versions/`` 目录,迁移实际落在
``backend/alembic/versions/0002_add_tool_events.py``(见回执说明)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, Session

from app.db import gen_id, utc_now
from app.models import Base

# 确认卡默认有效期:10 分钟超时默认拒绝(G7 卡)
CONFIRM_TTL_MINUTES = 10


class ToolEvent(Base):
    """一次工具调用产生的一条 trace 事件(append-only)。

    契约(03§4.2.3 Event 三件套):``{type, ts, trace_id, step, payload}``;
    此处再补 ``tool_name / ok / error_code`` 便于审计。payload 存 JSON 摘要。
    """

    __tablename__ = "tool_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="tool_call")
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("step >= 0", name="ck_tool_event_step_nonneg"),
        CheckConstraint("type = 'tool_call'", name="ck_tool_event_type"),
    )


class ConfirmRequest(Base):
    """写操作的人工确认卡(token + 四要素 + 10 分钟超时)。

    真正执行在 W3 ``invoke_confirmed(confirm_token)``;本任务书只落库 pending 卡。
    """

    __tablename__ = "confirm_request"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    confirm_token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True,
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # args 摘要 JSON
    what: Mapped[str] = mapped_column(Text, nullable=False)  # ①做什么
    impact: Mapped[str] = mapped_column(Text, nullable=False)  # ②影响面
    irreversibility: Mapped[str] = mapped_column(Text, nullable=False)  # ③不可逆性
    alternatives: Mapped[str] = mapped_column(Text, nullable=False)  # ④替代方案
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','confirmed','expired','cancelled')",
            name="ck_confirm_request_status",
        ),
    )


def _dumps(payload: Any) -> str:
    """payload 摘要序列化:中文不转义,非原生类型用 str 兜底。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def write_event(
    session: Session,
    *,
    trace_id: str,
    step: int,
    tool_name: str,
    ok: bool,
    error_code: str | None,
    payload: Any,
) -> ToolEvent:
    """写入一条 tool_call 事件(append-only)。调用方负责 commit。"""
    event = ToolEvent(
        trace_id=trace_id,
        step=step,
        tool_name=tool_name,
        ok=ok,
        error_code=error_code,
        payload=_dumps(payload),
    )
    session.add(event)
    return event


def generate_confirm_token() -> str:
    """确认卡 token:uuid4 去横线(32 hex),落库唯一。"""
    return uuid4().hex


def create_confirm(
    session: Session,
    *,
    tool_name: str,
    args: dict[str, Any],
    card: dict[str, str],
    ttl_minutes: int = CONFIRM_TTL_MINUTES,
) -> ConfirmRequest:
    """生成确认卡并落库(四要素 + token),返回 ConfirmRequest。调用方负责 commit。"""
    token = generate_confirm_token()
    confirm = ConfirmRequest(
        confirm_token=token,
        tool_name=tool_name,
        payload=_dumps({"args": args}),
        what=card["what"],
        impact=card["impact"],
        irreversibility=card["irreversibility"],
        alternatives=card["alternatives"],
        status="pending",
        expires_at=utc_now() + timedelta(minutes=ttl_minutes),
    )
    session.add(confirm)
    return confirm


def get_confirm(session: Session, confirm_token: str) -> ConfirmRequest | None:
    """按 token 查确认卡(供 W3 invoke_confirmed 使用)。"""
    return session.scalar(
        select(ConfirmRequest).where(ConfirmRequest.confirm_token == confirm_token)
    )
