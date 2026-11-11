"""工单 API:GET 列表/单个、POST 创建、claim、close、消息。

claim/close/add_message 全部委托 services.tickets 的守卫式实现;
关单后 add_message 返回 409 STATE_CONFLICT。

W13 ISS-60:该组路由原先无任何身份校验与租户过滤,现统一挂 ``current_identity``
并按 ``identity.merchant_id`` 隔离(internal 模式不过滤);买家身份不能
冒充人工坐席写入 ``author_type="agent"`` 的消息。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import NotFoundError
from app.models import Ticket, TicketMessage
from app.schemas import (
    Receipt,
    TicketClaim,
    TicketCreate,
    TicketMessageIn,
    TicketMessageOut,
    TicketOut,
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


@router.post("/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
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
) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    return ticket


@router.post("/tickets/{ticket_id}/claim", response_model=Receipt)
def claim(
    ticket_id: str,
    payload: TicketClaim,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> Receipt:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    ticket = ticket_svc.claim(db, ticket_id, payload.service_id)
    return Receipt(
        ticket_id=ticket.id,
        status=ticket.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/tickets/{ticket_id}/close", response_model=Receipt)
def close(
    ticket_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
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


@router.get("/tickets/{ticket_id}/messages", response_model=list[TicketMessageOut])
def list_messages(
    ticket_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> list[TicketMessage]:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    _assert_tenant(identity, ticket)
    return list(
        db.execute(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at)
        ).scalars()
    )
