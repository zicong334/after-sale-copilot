"""api/handoff.py — 转人工全链路 API(W3-C)。

契约(任务书 W3-C + G7 卡):

- ``GET  /api/c/handoff/tickets?merchant_id=&status=``:工单列表,queued 优先。
- ``POST /api/c/handoff/tickets/{id}/claim``:客服领单(守卫式,被抢 → 409
  "已被他人领取")。
- ``POST /api/c/handoff/tickets/{id}/close``:关单(guard:不可重复关单)。
- ``GET  /api/c/handoff/sessions/{session_id}/summary``:会话摘要(诉求/会话状态/
  工单/最近消息),供坐席接手时一眼看清上下文。

AI 停止回复的判定点在 chat 入口(session.status != active → build_blocked_turn,
不跑图);本模块只负责人工侧的队列与状态推进。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import AppError, NotFoundError, StateConflictError
from app.models import ChatMessage, ChatSession, Ticket, TicketMessage
from app.services import tickets as ticket_svc

router = APIRouter(prefix="/api/c/handoff", tags=["handoff"])

_STATUSES = ("queued", "claimed", "waiting_customer", "closed")


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: str


class CloseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: str | None = None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.isoformat()


def _ticket_dict(ticket: Ticket) -> dict[str, Any]:
    return {
        "ticket_id": ticket.id,
        "merchant_id": ticket.merchant_id,
        "customer_id": ticket.customer_id,
        "order_id": ticket.order_id,
        "session_id": ticket.session_id,
        "status": ticket.status,
        "claimed_by": ticket.claimed_by,
        "handoff_reason": ticket.handoff_reason,
        "summary": ticket.summary,
        "created_at_utc": _iso(ticket.created_at),
        "last_msg_at_utc": _iso(ticket.last_msg_at),
    }


def _receipt(ticket: Ticket) -> dict[str, Any]:
    return {
        "ticket_id": ticket.id,
        "status": ticket.status,
        "claimed_by": ticket.claimed_by,
        "occurred_at_utc": _iso(utc_now()),
        "verified": ticket.status in ("claimed", "closed"),
    }


@router.get("/tickets")
def list_tickets(
    merchant_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """工单队列:queued 优先,同状态内按创建时间先后。"""
    stmt = select(Ticket)
    if merchant_id:
        stmt = stmt.where(Ticket.merchant_id == merchant_id)
    if status:
        if status not in _STATUSES:
            raise StateConflictError(f"未知工单状态: {status}", None, param="status")
        stmt = stmt.where(Ticket.status == status)
    stmt = stmt.order_by(case((Ticket.status == "queued", 0), else_=1), Ticket.created_at.asc())
    rows = list(db.scalars(stmt).all())
    return {
        "ok": True,
        "items": [_ticket_dict(row) for row in rows],
        "total": len(rows),
        "queued_first": True,
    }


@router.post("/tickets/{ticket_id}/claim")
def claim(ticket_id: str, payload: ClaimIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    """守卫式领单:并发只有一个成功,被抢返回 409「已被他人领取」。"""
    try:
        ticket = ticket_svc.claim(db, ticket_id, payload.service_id)
    except StateConflictError as exc:
        raise StateConflictError(
            f"工单已被他人领取或不可领取(当前状态:{exc.param})",
            exc.param,
            param="status",
        ) from exc
    return {"ok": True, "receipt": _receipt(ticket)}


@router.post("/tickets/{ticket_id}/close")
def close(ticket_id: str, payload: CloseIn | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    """关单:queued/claimed/waiting_customer → closed;重复关单 409。"""
    try:
        ticket = ticket_svc.close(db, ticket_id)
    except StateConflictError as exc:
        raise StateConflictError(
            f"工单已关闭或状态不可关闭(当前状态:{exc.param})",
            exc.param,
            param="status",
        ) from exc
    body = {"ok": True, "receipt": _receipt(ticket)}
    if payload is not None and payload.service_id:
        body["closed_by"] = payload.service_id
    return body


@router.get("/sessions/{session_id}/summary")
def session_summary(session_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """会话摘要:会话状态 + 本会话工单 + 最近消息(坐席接手用)。

    W5-FIX R1:工单按 ``ticket.session_id`` **精确归属**本会话;
    历史工单没有 session_id 时返回 ticket=None,绝不回退到
    「按客户取最近一张」的启发式(那会把同客户历史 closed 工单误当成当前会话)。
    """
    row = db.get(ChatSession, session_id)
    if row is None:
        raise NotFoundError(f"会话不存在: {session_id}")
    ticket = _session_ticket(db, session_id)
    return _summary_body(db, row, ticket, link="session_id(精确归属)")


def _session_ticket(db: Session, session_id: str) -> Ticket | None:
    """按 session_id 精确取本会话的工单(W5-FIX R1,不做客户级回退)。"""
    return db.scalars(
        select(Ticket)
        .where(Ticket.session_id == session_id)
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(1)
    ).first()


def _public_ticket_messages(db: Session, ticket: Ticket | None) -> list[dict[str, Any]]:
    """买家侧可见的工单消息:is_internal=True 的坐席内部备注不下发。"""
    if ticket is None:
        return []
    return [
        {
            "id": item.id,
            "author_type": item.author_type,
            "content": item.content,
            "is_internal": bool(item.is_internal),
            "created_at_utc": _iso(item.created_at),
        }
        for item in db.scalars(
            select(TicketMessage)
            .where(
                TicketMessage.ticket_id == ticket.id,
                TicketMessage.is_internal.is_(False),
            )
            .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
        ).all()
    ]


def _summary_body(
    db: Session, row: ChatSession, ticket: Ticket | None, *, link: str,
) -> dict[str, Any]:
    """会话摘要响应体(两个入口共用,保证形状一致)。"""
    messages = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == row.id)
            .order_by(ChatMessage.created_at.asc())
            .limit(20)
        ).all()
    )
    return {
        "ok": True,
        "session_id": row.id,
        "session_status": row.status,
        "merchant_id": row.merchant_id,
        "customer_id": row.customer_id,
        "ticket": _ticket_dict(ticket) if ticket is not None else None,
        "ticket_link": link,
        "ticket_status": ticket.status if ticket is not None else None,
        "ticket_closed": bool(ticket is not None and ticket.status == "closed"),
        "ticket_messages": _public_ticket_messages(db, ticket),
        "messages": [
            {
                "role": item.role,
                "content": item.content,
                "intent": item.intent,
                "trace_id": item.trace_id,
                "created_at_utc": _iso(item.created_at),
            }
            for item in messages
        ],
    }


@router.get("/summary")
def summary(
    session_id: str | None = Query(default=None),
    merchant_id: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """会话摘要兼容入口(买家页轮询用 ``?session_id=...``)。

    - 传 ``session_id``:与本会话摘要完全一致,只返回该会话的工单;
    - 不传 ``session_id``:保持旧行为——按 ``merchant_id+customer_id``
      取该客户最近一张工单(兼容既有调用方,新前端不再使用)。
    """
    if session_id:
        row = db.get(ChatSession, session_id)
        if row is None:
            raise NotFoundError(f"会话不存在: {session_id}")
        return _summary_body(
            db, row, _session_ticket(db, session_id), link="session_id(精确归属)",
        )
    if not merchant_id or not customer_id:
        raise AppError(
            "VALIDATION_ERROR",
            "缺少 session_id,或同时提供 merchant_id 与 customer_id",
            400,
            param="session_id",
        )
    legacy_ticket = db.scalars(
        select(Ticket)
        .where(Ticket.merchant_id == merchant_id, Ticket.customer_id == customer_id)
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(1)
    ).first()
    return {
        "ok": True,
        "session_id": None,
        "session_status": None,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "ticket": _ticket_dict(legacy_ticket) if legacy_ticket is not None else None,
        "ticket_link": "merchant_id+customer_id(最近一张;旧行为兼容)",
        "ticket_status": legacy_ticket.status if legacy_ticket is not None else None,
        "ticket_closed": bool(legacy_ticket is not None and legacy_ticket.status == "closed"),
        "ticket_messages": _public_ticket_messages(db, legacy_ticket),
        "messages": [],
    }
