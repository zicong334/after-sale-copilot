"""工单 API:GET 列表/单个、POST 创建、claim、close、消息。

claim/close/add_message 全部委托 services.tickets 的守卫式实现;
关单后 add_message 返回 409 STATE_CONFLICT。

W13 ISS-60:该组路由原先无任何身份校验与租户过滤,现统一挂 ``current_identity``
并按 ``identity.merchant_id`` 隔离(internal 模式不过滤);买家身份不能
冒充人工坐席写入 ``author_type="agent"`` 的消息。

A1(2026-11-16):ISS-60 只补了**认证**,本组仍是 ``ROLE_ANY`` 且只比租户,
于是买家 token 能**冒充坐席 service_id 领单并关单**。现把建单/领单/关单/状态流转
收归 agent(``require_agent``),``service_id`` 一律从 token 取。
买家留言(``add_message``)仍是正当能力,保持 ``current_identity``。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import get_db, require_agent
from app.db import utc_now
from app.errors import NotFoundError
from app.models import RefundRequest, Ticket, TicketMessage
from app.schemas import (
    Receipt,
    TicketClaim,
    TicketCreate,
    TicketMessageIn,
    TicketMessageOut,
    TicketOut,
    TicketStatusIn,
)
from app.security import Identity, current_identity
from app.services import tickets as ticket_svc

router = APIRouter(tags=["tickets"])


def _assert_tenant(identity: Identity, ticket: Ticket) -> None:
    """跨租户访问按"不存在"处理(不泄露他店工单是否存在)。"""
    if identity.is_internal:
        return
    if ticket.merchant_id != identity.merchant_id:
        raise NotFoundError(f"工单不存在: {ticket.id}")


def _service_id(identity: Identity, claimed: str | None) -> str:
    """领单/流转的坐席 id 一律取 token 主体(A1);请求体值无身份效力。

    internal 模式(回归脚本)保留请求体值,与 ``merchant_id`` 既有约定一致。
    """
    if identity.is_internal:
        return claimed or identity.sub
    return identity.service_id or identity.sub


@router.post("/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_agent),
) -> Ticket:
    merchant_id = payload.merchant_id
    if not identity.is_internal:
        # 非 internal 模式:租户只能来自 token,请求体里的 merchant_id 不可信。
        merchant_id = identity.merchant_id
    ticket = Ticket(
        merchant_id=merchant_id,
        customer_id=payload.customer_id,
        order_id=payload.order_id,
        status="queued",
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


@router.get("/tickets", response_model=list[TicketOut])
def list_tickets(
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> list[Ticket]:
    stmt = select(Ticket).order_by(Ticket.created_at.desc())
    if not identity.is_internal:
        stmt = stmt.where(Ticket.merchant_id == identity.merchant_id)
    return list(db.execute(stmt).scalars())


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> TicketOut:
    """工单详情:W11 B2 附带该订单最新退款单 id/状态。"""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    out = TicketOut.model_validate(ticket)
    refund = _latest_refund(db, ticket)
    if refund is not None:
        out.refund_request_id = refund.id
        out.refund_status = refund.status
    return out


def _latest_refund(db: Session, ticket: Ticket) -> RefundRequest | None:
    """工单关联订单的最新退款单(无订单/无退款单返回 None)。"""
    if not ticket.order_id:
        return None
    return db.execute(
        select(RefundRequest)
        .where(RefundRequest.order_id == ticket.order_id)
        .order_by(RefundRequest.created_at.desc(), RefundRequest.id.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.post("/tickets/{ticket_id}/status", response_model=Receipt)
def transition_status(
    ticket_id: str,
    payload: TicketStatusIn,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_agent),
) -> Receipt:
    """W11 B7:工单状态手动流转(仅 queued→claimed→waiting_customer→closed)。"""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    updated = ticket_svc.transition(
        db, ticket_id, payload.to_status, service_id=_service_id(identity, payload.service_id)
    )
    return Receipt(
        ticket_id=updated.id,
        status=updated.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/tickets/{ticket_id}/claim", response_model=Receipt)
def claim(
    ticket_id: str,
    payload: TicketClaim,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_agent),
) -> Receipt:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    ticket = ticket_svc.claim(db, ticket_id, _service_id(identity, payload.service_id))
    return Receipt(
        ticket_id=ticket.id,
        status=ticket.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/tickets/{ticket_id}/close", response_model=Receipt)
def close(
    ticket_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_agent),
) -> Receipt:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    ticket = ticket_svc.close(db, ticket_id)
    return Receipt(
        ticket_id=ticket.id,
        status=ticket.status,
        occurred_at_utc=utc_now(),
    )


@router.post(
    "/tickets/{ticket_id}/messages",
    response_model=TicketMessageOut,
    status_code=status.HTTP_201_CREATED,
)
def add_message(
    ticket_id: str,
    payload: TicketMessageIn,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> TicketMessage:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    author_type = payload.author_type
    if not identity.is_internal and identity.role == "buyer" and author_type != "user":
        # 买家不能冒充人工坐席/系统写工单消息。
        author_type = "user"
    return ticket_svc.add_message(
        db,
        ticket_id,
        author_type=author_type,
        content=payload.content,
        is_internal=payload.is_internal,
    )


@router.get("/tickets/{ticket_id}/messages")
def list_messages(
    ticket_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> list[dict]:
    """工单消息 + 转人工前聊天记录(W13-ISS-13 全量穿透)。

    返回结构在 ``TicketMessageOut`` 字段基础上追加 ``source``/``author_label``:
    ``source="chat"`` 的是转人工前的买家提问/AI 答复(前端用灰色机器人角标),
    ``source="ticket"`` 的是转人工后的工单消息。两者合并后按时间升序。
    """
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    items: list[dict] = []
    for row in ticket_svc.chat_prefix(db, ticket):
        items.append(
            {
                "id": f"chat:{row.id}",
                "ticket_id": ticket_id,
                "author_type": row.role,
                "author_label": "AI 助手" if row.role == "assistant" else "买家",
                "content": row.content,
                "is_internal": False,
                "created_at": row.created_at,
                "source": "chat",
            }
        )
    rows = list(
        db.execute(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at)
        ).scalars()
    )
    for row in rows:
        items.append(
            {
                "id": row.id,
                "ticket_id": row.ticket_id,
                "author_type": row.author_type,
                "author_label": {
                    "user": "买家",
                    "agent": "人工客服",
                    "system": "系统",
                }.get(row.author_type, row.author_type),
                "content": row.content,
                "is_internal": bool(row.is_internal),
                "created_at": row.created_at,
                "source": "ticket",
            }
        )
    items.sort(key=lambda item: (item["created_at"], item["id"]))
    return items
