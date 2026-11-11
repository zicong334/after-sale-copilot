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

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.db import utc_now
from app.errors import AppError, NotFoundError, StateConflictError
from app.models import ChatMessage, ChatSession, Ticket, TicketMessage
from app.services import tickets as ticket_svc

router = APIRouter(prefix="/api/c/handoff", tags=["handoff"])

_STATUSES = ("queued", "claimed", "waiting_customer", "closed")

# W15 U6:会话摘要视图粒度(full=全量兼容旧形状 / brief=轮询极简 / messages=增量消息)。
_SUMMARY_VIEWS = ("full", "brief", "messages")


def _check_view(view: str) -> str:
    if view not in _SUMMARY_VIEWS:
        raise AppError(
            "VALIDATION_ERROR",
            f"未知视图: {view}",
            400,
            param="view",
        )
    return view


class ClaimIn(BaseModel):
    """领单请求。

    ⚠️ ``service_id`` 仅 AUTH_MODE=internal 回归兜底;enforce 下无身份效力
    (坐席身份取自 token,见 ``security.effective_identity``)。
    """

    model_config = ConfigDict(extra="forbid")

    service_id: str | None = None


class CloseIn(BaseModel):
    """关单请求(可选 body)。

    ⚠️ ``service_id`` 仅 AUTH_MODE=internal 回归兜底;enforce 下无身份效力。
    """

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


def _scope_ticket(
    db: Session, ticket_id: str, identity: security.Identity, ident: security.Identity
) -> Ticket:
    """enforce 下校验工单归属:买家只能碰自己 merchant+customer 的工单。

    internal 模式跳过这条新过滤(回归脚本不带 token,靠请求体兜底),
    响应仍与旧版一致(不存在统一 404)。
    """
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    if not identity.is_internal and (
        ticket.merchant_id != ident.merchant_id
        or ticket.customer_id != ident.customer_id
    ):
        raise NotFoundError(f"工单不存在: {ticket_id}")
    return ticket


def _scope_session(
    row: ChatSession, identity: security.Identity, ident: security.Identity
) -> None:
    """enforce 下校验会话归属:买家只能看自己的会话;internal 跳过。"""
    if not identity.is_internal and (
        row.merchant_id != ident.merchant_id or row.customer_id != ident.customer_id
    ):
        raise NotFoundError(f"会话不存在: {row.id}")


def _publish_ticket_events(
    ticket: Ticket, *, claimed_by: str | None = None, closed_by: str | None = None
) -> None:
    """写库 commit 之后广播工单事件(ticket_status → buyer;ticket_update → queue)。

    buyer 频道需要 session_id,拿不到时跳过买家侧(库内历史工单可能没有)。
    """
    buyer_payload: dict[str, Any] = {
        "ticket_id": ticket.id,
        "session_id": ticket.session_id,
        "status": ticket.status,
    }
    if claimed_by:
        buyer_payload["claimed_by"] = claimed_by
    if closed_by:
        buyer_payload["closed_by"] = closed_by
    if ticket.session_id:
        security.publish_event(
            security.channel_buyer(ticket.session_id), "ticket_status", **buyer_payload
        )
    queue_payload: dict[str, Any] = {"ticket_id": ticket.id, "status": ticket.status}
    if claimed_by:
        queue_payload["claimed_by"] = claimed_by
    if closed_by:
        queue_payload["closed_by"] = closed_by
    security.publish_event(
        security.channel_queue(ticket.merchant_id), "ticket_update", **queue_payload
    )


@router.get("/tickets")
def list_tickets(
    request: Request,
    merchant_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """工单队列:queued 优先,同状态内按创建时间先后。

    enforce 下强制按 token 的 merchant_id 过滤(query 的同名字段无身份效力);
    internal 保持旧行为。
    """
    ident = security.effective_identity(
        identity, "buyer", fallback=dict(request.query_params)
    )
    stmt = select(Ticket)
    scope_merchant = merchant_id if identity.is_internal else ident.merchant_id
    if scope_merchant:
        stmt = stmt.where(Ticket.merchant_id == scope_merchant)
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


@router.post(
    "/tickets/{ticket_id}/claim",
    dependencies=[Depends(security.require_replay_guard)],
)
def claim(
    ticket_id: str,
    payload: ClaimIn,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """守卫式领单:并发只有一个成功,被抢返回 409「已被他人领取」。"""
    ident = security.effective_identity(identity, "buyer", payload=payload)
    # internal 回归兜底才读 body;enforce 下坐席身份只认 token,防止借 body 冒领。
    service_id = (
        (ident.service_id or payload.service_id)
        if identity.is_internal
        else ident.service_id
    )
    if not service_id:
        raise AppError(
            "VALIDATION_ERROR", "缺少坐席身份 service_id", 400, param="service_id"
        )
    _scope_ticket(db, ticket_id, identity, ident)
    try:
        ticket = ticket_svc.claim(db, ticket_id, service_id)
    except StateConflictError as exc:
        raise StateConflictError(
            f"工单已被他人领取或不可领取(当前状态:{exc.param})",
            exc.param,
            param="status",
        ) from exc
    # commit 已由 ticket_svc 完成,这里才发布事件。
    _publish_ticket_events(ticket, claimed_by=service_id)
    return {"ok": True, "receipt": _receipt(ticket)}


@router.post(
    "/tickets/{ticket_id}/close",
    dependencies=[Depends(security.require_replay_guard)],
)
def close(
    ticket_id: str,
    payload: CloseIn | None = None,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """关单:queued/claimed/waiting_customer → closed;重复关单 409。"""
    ident = security.effective_identity(identity, "buyer", payload=payload)
    _scope_ticket(db, ticket_id, identity, ident)
    # internal 回归兜底才读 body;enforce 下关单人只认 token。
    closed_by = (
        (ident.service_id or (payload.service_id if payload is not None else None))
        if identity.is_internal
        else ident.service_id
    )
    try:
        ticket = ticket_svc.close(db, ticket_id)
    except StateConflictError as exc:
        raise StateConflictError(
            f"工单已关闭或状态不可关闭(当前状态:{exc.param})",
            exc.param,
            param="status",
        ) from exc
    body = {"ok": True, "receipt": _receipt(ticket)}
    if closed_by:
        body["closed_by"] = closed_by
    # commit 已由 ticket_svc 完成。
    _publish_ticket_events(ticket, closed_by=closed_by)
    return body


@router.get("/sessions/{session_id}/summary")
def session_summary(
    session_id: str,
    view: str = Query(default="full"),
    after: str | None = Query(default=None),
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """会话摘要:会话状态 + 本会话工单 + 最近消息(坐席接手用)。

    W5-FIX R1:工单按 ``ticket.session_id`` **精确归属**本会话;
    历史工单没有 session_id 时返回 ticket=None,绝不回退到
    「按客户取最近一张」的启发式(那会把同客户历史 closed 工单误当成当前会话)。
    enforce 下买家只能看自己的会话(internal 跳过)。

    W15 U6:``view=brief`` 返回极简字段(轮询用,不含消息正文);
    ``view=messages`` 只回 ``after`` 之后的新工单消息。
    """
    ident = security.effective_identity(identity, "buyer")
    row = db.get(ChatSession, session_id)
    if row is None:
        raise NotFoundError(f"会话不存在: {session_id}")
    _scope_session(row, identity, ident)
    ticket = _session_ticket(db, session_id)
    return _summary_body(
        db, row, ticket, link="session_id(精确归属)",
        view=_check_view(view), after=after,
    )


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


def _ticket_meta(ticket: Ticket | None) -> dict[str, Any] | None:
    """极简工单视图(W15 U6):轮询只需要这几项,不回传摘要正文等大字段。"""
    if ticket is None:
        return None
    return {
        "ticket_id": ticket.id,
        "order_id": ticket.order_id,
        "status": ticket.status,
        "created_at_utc": _iso(ticket.created_at),
        "last_msg_at_utc": _iso(ticket.last_msg_at),
    }


def _ticket_message_stats(db: Session, ticket: Ticket | None) -> tuple[str | None, int]:
    """工单最后消息时间 + 可见消息条数(W15 U6 极简轮询用)。"""
    if ticket is None:
        return None, 0
    count = int(
        db.scalar(
            select(func.count(TicketMessage.id)).where(
                TicketMessage.ticket_id == ticket.id,
                TicketMessage.is_internal.is_(False),
            )
        )
        or 0
    )
    return _iso(ticket.last_msg_at), count


def _parse_after(value: str | None) -> datetime | None:
    """解析 ``after`` 游标(ISO 时间串)→ naive UTC(与库里 created_at 对齐)。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _summary_body(
    db: Session, row: ChatSession, ticket: Ticket | None, *, link: str,
    view: str = "full", after: str | None = None,
) -> dict[str, Any]:
    """会话摘要响应体(两个入口共用,保证形状一致)。"""
    return _build_summary(
        db,
        session_id=row.id,
        session_status=row.status,
        merchant_id=row.merchant_id,
        customer_id=row.customer_id,
        ticket=ticket,
        link=link,
        view=view,
        after=after,
    )


def _build_summary(
    db: Session,
    *,
    session_id: str | None,
    session_status: str | None,
    merchant_id: str | None,
    customer_id: str | None,
    ticket: Ticket | None,
    link: str,
    view: str = "full",
    after: str | None = None,
) -> dict[str, Any]:
    """会话摘要响应体(按视图粒度裁剪)。

    W15 U6:``view`` 控制返回粒度——
    - ``full``(默认):保持既有形状(messages + ticket_messages 全量),兼容既有调用方;
    - ``brief``:轮询用的极简视图,只回状态与计数,**不含任何消息正文**;
    - ``messages``:增量消息视图,只回 ``after`` 之后的新工单消息。
    """
    if view == "brief":
        last_msg_at, message_count = _ticket_message_stats(db, ticket)
        return {
            "ok": True,
            "view": "brief",
            "session_id": session_id,
            "session_status": session_status,
            "ticket": _ticket_meta(ticket),
            "ticket_link": link,
            "ticket_status": ticket.status if ticket is not None else None,
            "ticket_closed": bool(ticket is not None and ticket.status == "closed"),
            "last_msg_at_utc": last_msg_at,
            "message_count": message_count,
        }
    if view == "messages":
        last_msg_at, message_count = _ticket_message_stats(db, ticket)
        cursor = _parse_after(after)
        messages = _public_ticket_messages(db, ticket)
        if cursor is not None:
            messages = [
                item
                for item in messages
                if (_parse_after(item.get("created_at_utc")) or cursor) >= cursor
            ]
        return {
            "ok": True,
            "view": "messages",
            "session_id": session_id,
            "session_status": session_status,
            "ticket_status": ticket.status if ticket is not None else None,
            "ticket_closed": bool(ticket is not None and ticket.status == "closed"),
            "last_msg_at_utc": last_msg_at,
            "message_count": message_count,
            "ticket_messages": messages,
        }
    messages = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
            .limit(20)
        ).all()
    )
    return {
        "ok": True,
        "session_id": session_id,
        "session_status": session_status,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
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
    view: str = Query(default="full"),
    after: str | None = Query(default=None),
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """会话摘要兼容入口(买家页轮询用 ``?session_id=...``)。

    - 传 ``session_id``:与本会话摘要完全一致,只返回该会话的工单;
    - 不传 ``session_id``:保持旧行为——按 ``merchant_id+customer_id``
      取该客户最近一张工单(兼容既有调用方,新前端不再使用)。
    enforce 下 query 里的 merchant_id/customer_id 无身份效力,一律取 token 身份;
    internal 保持旧行为。
    """
    ident = security.effective_identity(identity, "buyer")
    if not identity.is_internal:
        merchant_id = ident.merchant_id
        customer_id = ident.customer_id
    wanted_view = _check_view(view)
    if session_id:
        row = db.get(ChatSession, session_id)
        if row is None:
            raise NotFoundError(f"会话不存在: {session_id}")
        _scope_session(row, identity, ident)
        return _summary_body(
            db, row, _session_ticket(db, session_id), link="session_id(精确归属)",
            view=wanted_view, after=after,
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
    if wanted_view != "full":
        return _build_summary(
            db,
            session_id=None,
            session_status=None,
            merchant_id=merchant_id,
            customer_id=customer_id,
            ticket=legacy_ticket,
            link="merchant_id+customer_id(最近一张;旧行为兼容)",
            view=wanted_view,
            after=after,
        )
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
