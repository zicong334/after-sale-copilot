"""工单服务:claim / close / add_message,守卫式;关单后 add_message 拒绝。

迁移表(03§4.1.3):
- queued → claimed(客服领单,``WHERE status='queued'``,rowcount=1 才算成功)
- queued/claimed/waiting_customer → closed(关单后 add_message 拒绝)
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import utc_now
from app.errors import NotFoundError, StateConflictError
from app.models import ChatMessage, Ticket, TicketMessage

_CLOSABLE = ("queued", "claimed", "waiting_customer")


def claim(session: Session, ticket_id: str, service_id: str) -> Ticket:
    """客服领单:queued → claimed。两个客服并发领同一单只有一个成功。"""
    result = session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status == "queued")
        .values(status="claimed", claimed_by=service_id, last_msg_at=utc_now())
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(Ticket, ticket_id)
        if current is None:
            raise NotFoundError(f"工单不存在: {ticket_id}")
        raise StateConflictError("工单已被领取或不可领取", current.status)
    session.commit()
    return session.get(Ticket, ticket_id)


def close(session: Session, ticket_id: str) -> Ticket:
    """关单:queued/claimed/waiting_customer → closed。

    注:模型无 closed_at/closed_by 字段(W1-A 冻结),此处只推进 status + last_msg_at,
    字段缺失作为模型缺陷在回执中汇报,不自行改模型。
    """
    result = session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status.in_(_CLOSABLE))
        .values(status="closed", last_msg_at=utc_now())
    )
    if result.rowcount != 1:
        session.rollback()
        current = session.get(Ticket, ticket_id)
        if current is None:
            raise NotFoundError(f"工单不存在: {ticket_id}")
        raise StateConflictError("工单已关闭或状态不可关闭", current.status)
    session.commit()
    return session.get(Ticket, ticket_id)


def add_message(
    session: Session,
    ticket_id: str,
    author_type: str,
    content: str,
    is_internal: bool = False,
) -> TicketMessage:
    """添加工单消息;关单后拒绝(409)。

    G2 守卫式:先用带状态条件的 UPDATE 在同一事务里"校验 + 加写锁 + 刷新
    last_msg_at",rowcount=1 才继续 INSERT,消灭"先 SELECT 后 INSERT"的
    TOCTOU 窗口(并发 close 提交后,本条 UPDATE 必然 rowcount=0 而拒绝)。
    """
    guard = session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status != "closed")
        .values(last_msg_at=utc_now())
    )
    if guard.rowcount != 1:
        session.rollback()
        current = session.get(Ticket, ticket_id)
        if current is None:
            raise NotFoundError(f"工单不存在: {ticket_id}")
        raise StateConflictError("工单已关闭,不能添加消息", current.status)

    message = TicketMessage(
        ticket_id=ticket_id,
        author_type=author_type,
        content=content,
        is_internal=is_internal,
    )
    session.add(message)
    session.commit()
    session.refresh(message)
    return message


def chat_prefix(session: Session, ticket: Ticket) -> list[ChatMessage]:
    """工单创建前的聊天记录(W13-ISS-13 转录穿透)。

    按 ``ticket.session_id`` 联查 ``chat_message``,只取 ``created_at <=
    ticket.created_at``:转人工后的买家留言已由路由守卫追加进
    ``ticket_message``,再取一次会重复。工单无 session_id(历史数据)时返回空。
    """
    if not ticket.session_id:
        return []
    return list(
        session.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == ticket.session_id,
                ChatMessage.created_at <= ticket.created_at,
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        ).all()
    )
