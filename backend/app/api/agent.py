"""api/agent.py — W5 客服工作台(B 端)路由。

设计要点(与任务书 W5 修正二一致):

- 业务只复用 ``app.services.tickets`` 的守卫式实现(claim / close / add_message),
  本模块只做"取数 + 投影成前端结构 + 错误体翻译",**不重写状态机**;
- 领单/回复/关单的并发守卫、关单后拒绝等语义与 ``/api/c/handoff`` 完全一致,
  只是换成了工作台需要的扁平结构(带中文标签、消息条数、最后一条消息);
- ``demo_no_password``:演示环境没有坐席登录/鉴权,``service_id`` 仅用于回执归属,
  这一点在响应里显式声明,避免被误当成已实现的权限体系。

路由一览(prefix ``/api/b``):

- ``GET  /services``:可用坐席列表(演示环境固定两名 seed 坐席);
- ``GET  /tickets``:工单队列(queued 优先 → last_msg_at DESC → created_at DESC);
- ``GET  /tickets/{id}``:工单详情 + 全量消息(created_at asc);
- ``POST /tickets/{id}/claim``:领单(被抢 → 409「已被他人领取」);
- ``POST /tickets/{id}/messages``:坐席回复(author_type=agent,关单后 409);
- ``POST /tickets/{id}/close``:关单(重复关单 409)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import AppError, NotFoundError, StateConflictError
from app.guardrails import handoff_reason_label
from app.models import Ticket, TicketMessage
from app.services import tickets as ticket_svc

router = APIRouter(prefix="/api/b", tags=["agent-workbench"])

#: 演示环境固定坐席(与 scripts/seed.py 的 AGENT_1/AGENT_2 对齐,无登录/无密码)。
_SERVICES: tuple[dict[str, str], ...] = (
    {"service_id": "seed-agent-1", "display_name": "客服 小暖"},
    {"service_id": "seed-agent-2", "display_name": "客服 小满"},
)

#: 工单状态 → 工作台中文标签(status 枚举来自 W1-A 冻结模型)。
_STATUS_LABELS: dict[str, str] = {
    "queued": "待领取",
    "claimed": "跟进中",
    "waiting_customer": "待客户回复",
    "closed": "已关闭",
}

#: 消息作者类型 → 中文标签(ticket_message.author_type 的 CHECK 只有这三种)。
_AUTHOR_LABELS: dict[str, str] = {
    "user": "买家",
    "agent": "人工客服",
    "system": "系统",
}


class ClaimIn(BaseModel):
    """领单请求:service_id 只用于回执归属,不校验登录态(演示环境)。"""

    model_config = ConfigDict(extra="forbid")

    service_id: str = Field(min_length=1, max_length=64)


class MessageIn(BaseModel):
    """坐席回复:content 空白由路由层挡成 400 VALIDATION_ERROR。"""

    model_config = ConfigDict(extra="forbid")

    service_id: str = Field(min_length=1, max_length=64)
    content: str
    is_internal: bool = False


class CloseIn(BaseModel):
    """关单请求:service_id 可选(关单动作以工单状态推进为准)。"""

    model_config = ConfigDict(extra="forbid")

    service_id: str | None = None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.isoformat()


def _message_dict(message: TicketMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "ticket_id": message.ticket_id,
        "author_type": message.author_type,
        "author_label": _AUTHOR_LABELS.get(message.author_type, message.author_type),
        "content": message.content,
        "is_internal": bool(message.is_internal),
        "created_at_utc": _iso(message.created_at),
    }


def _ticket_dict(
    ticket: Ticket,
    *,
    message_count: int = 0,
    last_message: str | None = None,
) -> dict[str, Any]:
    """工单 → 工作台结构(列表与详情共用,字段与契约一致)。"""
    reason = ticket.handoff_reason
    return {
        "ticket_id": ticket.id,
        "merchant_id": ticket.merchant_id,
        "customer_id": ticket.customer_id,
        "order_id": ticket.order_id,
        "status": ticket.status,
        "status_label": _STATUS_LABELS.get(ticket.status, ticket.status),
        "claimed_by": ticket.claimed_by,
        "handoff_reason": reason,
        "handoff_reason_label": handoff_reason_label(reason) if reason else None,
        "summary": ticket.summary,
        "created_at_utc": _iso(ticket.created_at),
        "last_msg_at_utc": _iso(ticket.last_msg_at),
        "message_count": message_count,
        "last_message": last_message,
    }


def _receipt(ticket: Ticket) -> dict[str, Any]:
    """动作回执:verified 表示"目标状态已真实落到库里"。"""
    return {
        "ticket_id": ticket.id,
        "status": ticket.status,
        "claimed_by": ticket.claimed_by,
        "occurred_at_utc": _iso(utc_now()),
        "verified": ticket.status in ("claimed", "closed"),
    }


def _get_ticket(db: Session, ticket_id: str) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    return ticket


def _queue_stats(db: Session, tickets: list[Ticket]) -> dict[str, tuple[int, str | None]]:
    """批量取每张工单的消息条数与最后一条内容(避免列表 N+1 逐单查)。"""
    if not tickets:
        return {}
    ids = [ticket.id for ticket in tickets]
    counts = dict(
        db.execute(
            select(TicketMessage.ticket_id, func.count())
            .where(TicketMessage.ticket_id.in_(ids))
            .group_by(TicketMessage.ticket_id)
        ).all()
    )
    latest: dict[str, str] = {}
    for message in db.scalars(
        select(TicketMessage)
        .where(TicketMessage.ticket_id.in_(ids))
        .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
    ):
        latest[message.ticket_id] = message.content
    return {
        ticket_id: (int(counts.get(ticket_id, 0)), latest.get(ticket_id))
        for ticket_id in ids
    }


@router.get("/services")
def list_services() -> dict[str, Any]:
    """可用坐席:演示环境固定两名 seed 坐席,无登录/无密码。"""
    return {
        "ok": True,
        "items": [dict(item) for item in _SERVICES],
        "demo_no_password": True,
    }


@router.get("/tickets")
def list_tickets(
    merchant_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """工单队列:queued 优先 → 组内 last_msg_at DESC(NULL 最后)→ created_at DESC。"""
    stmt = select(Ticket)
    if merchant_id:
        stmt = stmt.where(Ticket.merchant_id == merchant_id)
    if status:
        if status not in _STATUS_LABELS:
            raise AppError(
                "VALIDATION_ERROR", f"未知工单状态: {status}", status_code=400, param="status"
            )
        stmt = stmt.where(Ticket.status == status)
    stmt = stmt.order_by(
        case((Ticket.status == "queued", 0), else_=1),
        # 非 NULL 优先(避免依赖 "NULLS LAST" 语法),再按最近消息倒序
        Ticket.last_msg_at.is_(None),
        Ticket.last_msg_at.desc(),
        Ticket.created_at.desc(),
    )
    rows = list(db.scalars(stmt).all())
    stats = _queue_stats(db, rows)
    items = []
    for row in rows:
        count, last_message = stats.get(row.id, (0, None))
        items.append(
            _ticket_dict(row, message_count=count, last_message=last_message)
        )
    return {"ok": True, "items": items, "total": len(rows), "queued_first": True}


@router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """工单详情:工单结构 + 全量消息(created_at asc)。"""
    ticket = _get_ticket(db, ticket_id)
    messages = list(
        db.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
        ).all()
    )
    return {
        "ok": True,
        "ticket": _ticket_dict(
            ticket,
            message_count=len(messages),
            last_message=(messages[-1].content if messages else None),
        ),
        "messages": [_message_dict(message) for message in messages],
    }


@router.post("/tickets/{ticket_id}/claim")
def claim_ticket(
    ticket_id: str, payload: ClaimIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
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


@router.post("/tickets/{ticket_id}/messages")
def add_ticket_message(
    ticket_id: str, payload: MessageIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """坐席回复:author_type=agent;关单后由服务守卫拒绝(409)。"""
    content = str(payload.content or "").strip()
    if not content:
        raise AppError(
            "VALIDATION_ERROR", "回复内容不能为空", status_code=400, param="content"
        )
    _get_ticket(db, ticket_id)
    try:
        message = ticket_svc.add_message(
            db, ticket_id, "agent", content, is_internal=payload.is_internal
        )
    except StateConflictError as exc:
        raise StateConflictError(
            f"工单已关闭,不能添加消息(当前状态:{exc.param})",
            exc.param,
            param="status",
        ) from exc
    return {"ok": True, "message": _message_dict(message)}


@router.post("/tickets/{ticket_id}/close")
def close_ticket(
    ticket_id: str, payload: CloseIn | None = None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """关单:queued/claimed/waiting_customer → closed;重复关单 409。"""
    try:
        ticket = ticket_svc.close(db, ticket_id)
    except StateConflictError as exc:
        raise StateConflictError(
            f"工单已关闭或状态不可关闭(当前状态:{exc.param})",
            exc.param,
            param="status",
        ) from exc
    body: dict[str, Any] = {"ok": True, "receipt": _receipt(ticket)}
    if payload is not None and payload.service_id:
        body["closed_by"] = payload.service_id
    return body
