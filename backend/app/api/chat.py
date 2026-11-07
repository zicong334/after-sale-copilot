"""api/chat.py — 会话薄路由 ``POST /api/c/chat``(W2-C 新增)。

路由只做"无业务"的四件事:
1. 请求校验 + 感知归一化(``app.perception``),非法输入 400;
2. 取/建 ``chat_session``,并做**会话状态守卫**:非 active 直接固定话术、不调编排
   (G7 判定点:转人工后 AI 停止自动回复,防止人工已接手还在自动答);
3. 落库 user 消息(trace_id / attachments) → 调编排 → 落库 assistant 消息(trace_id / intent);
4. 原样返回编排结果(intent / citations / pending_confirm / tool_calls / trace_id)。

业务判定(意图、工具调用顺序、条款引用、转人工条件)全部在
``app.orchestrator.w1_router``;本文件不出现任何工具名与政策规则,便于 W3 换图时
路由零改动。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api import get_db
from app.errors import AppError, NotFoundError
from app.models import ChatMessage, ChatSession
from app.graph import runner
from app.perception import PerceptionError, normalize
from app.trace import new_trace_id

router = APIRouter(prefix="/api/c", tags=["chat"])


class ChatRequest(BaseModel):
    """进线请求。

    ``context`` 是会话槽位(已由上游/上一轮解析出的确定性字段),不是感知层内容:
    ``order_id`` / ``days_since_delivery`` / ``unresolved_rounds`` /
    ``claimed_refund_cents`` / ``claimed_paid_cents`` / ``refund_amount_cents``。
    """

    model_config = ConfigDict(extra="forbid")

    merchant_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)
    text: str
    attachments: list[str] | None = None
    locale: str | None = None
    channel: str | None = None
    context: dict[str, Any] | None = None


@router.post("/chat")
def chat(payload: ChatRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """一轮对话:感知 → 落库 → 编排 → 落库 → 返回。"""
    try:
        percept = normalize(
            payload.text,
            attachments=payload.attachments,
            locale=payload.locale,
            channel=payload.channel,
        )
    except PerceptionError as exc:
        raise AppError(
            "VALIDATION_ERROR", str(exc), status_code=400, param=exc.field
        ) from exc

    # trace_id 归属本轮:ChatSession 表(W2-B 冻结,无 trace_id 列)不存该值,
    # 而是随本轮 user/assistant 两条 chat_message 与 tool_events 一起落库,
    # 验收要求的"会话带 trace_id"按"消息 + 事件同 trace_id 可反查"口径满足。
    trace_id = new_trace_id()
    session_row = _resolve_session(db, payload)

    user_msg = ChatMessage(
        session_id=session_row.id,
        role="user",
        content=percept.text,
        attachments=list(percept.attachments),
        trace_id=trace_id,
    )
    db.add(user_msg)
    db.flush()

    if session_row.status != "active":
        # 会话已被人工接手/关闭:记录一条拦截事件后停止自动回复。
        result = runner.build_blocked_turn(
            session=db, session_row=session_row, trace_id=trace_id, text=percept.text
        )
    else:
        result = runner.handle_turn(
            session=db,
            session_row=session_row,
            percept_text=percept.text,
            context=payload.context,
            trace_id=trace_id,
        )

    user_msg.intent = result.intent
    db.add(
        ChatMessage(
            session_id=session_row.id,
            role="assistant",
            content=result.reply,
            attachments=[],
            trace_id=trace_id,
            intent=result.intent,
        )
    )
    db.commit()

    body = result.to_dict()
    body.update(
        {
            "ok": True,
            "session_id": session_row.id,
            "trace_id": trace_id,
            "truncated": percept.truncated,
            "original_length": percept.original_length,
        }
    )
    return body


def _resolve_session(db: Session, payload: ChatRequest) -> ChatSession:
    """按 session_id 取会话,或为新客户开一个 active 会话(归属必须匹配)。"""
    if payload.session_id:
        session_row = db.get(ChatSession, payload.session_id)
        if (
            session_row is None
            or session_row.merchant_id != payload.merchant_id
            or session_row.customer_id != payload.customer_id
        ):
            raise NotFoundError("会话不存在或不属于该客户")
        return session_row
    session_row = ChatSession(
        merchant_id=payload.merchant_id,
        customer_id=payload.customer_id,
        status="active",
    )
    db.add(session_row)
    db.flush()
    return session_row
