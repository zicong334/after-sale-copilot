"""工单 API:GET 列表/单个、POST 创建、claim、close、消息。

claim/close/add_message 全部委托 services.tickets 的守卫式实现;
关单后 add_message 返回 409 STATE_CONFLICT。
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
from app.services import tickets as ticket_svc

router = APIRouter(tags=["tickets"])


@router.post("/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(payload: TicketCreate, db: Session = Depends(get_db)) -> Ticket:
    ticket = Ticket(
        merchant_id=payload.merchant_id,
        customer_id=payload.customer_id,
        order_id=payload.order_id,
        status="queued",
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


@router.get("/tickets", response_model=list[TicketOut])
def list_tickets(db: Session = Depends(get_db)) -> list[Ticket]:
    return list(
        db.execute(select(Ticket).order_by(Ticket.created_at.desc())).scalars()
    )


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: str, db: Session = Depends(get_db)) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    return ticket


@router.post("/tickets/{ticket_id}/claim", response_model=Receipt)
def claim(
    ticket_id: str, payload: TicketClaim, db: Session = Depends(get_db)
) -> Receipt:
    ticket = ticket_svc.claim(db, ticket_id, payload.service_id)
    return Receipt(
        ticket_id=ticket.id,
        status=ticket.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/tickets/{ticket_id}/close", response_model=Receipt)
def close(ticket_id: str, db: Session = Depends(get_db)) -> Receipt:
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
    ticket_id: str, payload: TicketMessageIn, db: Session = Depends(get_db)
) -> TicketMessage:
    return ticket_svc.add_message(
        db,
        ticket_id,
        author_type=payload.author_type,
        content=payload.content,
        is_internal=payload.is_internal,
    )


@router.get("/tickets/{ticket_id}/messages", response_model=list[TicketMessageOut])
def list_messages(ticket_id: str, db: Session = Depends(get_db)) -> list[TicketMessage]:
    return list(
        db.execute(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at)
        ).scalars()
    )
