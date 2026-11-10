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
- ``POST /tickets/{id}/close``:关单(重复关单 409);
- ``POST /tickets/{id}/suggest``:Copilot 建议回复(W7-4,规则模板 MOCK);
- ``GET  /tickets/{id}/suggest``:读取最近一次建议(进程内缓存,只读无副作用)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.db import utc_now
from app.errors import AppError, NotFoundError, StateConflictError
from app.guardrails import (
    handoff_reason_label,
    has_commitment_claim,
    strip_commitment_sentences,
)
from app.models import KnowledgeItem, Ticket, TicketMessage
from app.retrieval.base import RetrievedChunk
from app.retrieval.bm25_backend import DEFAULT_THRESHOLD, BM25Backend
from app.retrieval.chunker import PolicyChunk
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
    """领单请求。

    ⚠️ ``service_id`` 仅 AUTH_MODE=internal 回归兜底;enforce 下无身份效力
    (坐席身份取自 token,见 ``security.effective_identity``)。
    """

    model_config = ConfigDict(extra="forbid")

    service_id: str | None = Field(default=None, min_length=1, max_length=64)


class MessageIn(BaseModel):
    """坐席回复:content 空白由路由层挡成 400 VALIDATION_ERROR。

    ⚠️ ``service_id`` 仅 AUTH_MODE=internal 回归兜底;enforce 下无身份效力。
    """

    model_config = ConfigDict(extra="forbid")

    service_id: str | None = Field(default=None, min_length=1, max_length=64)
    content: str
    is_internal: bool = False


class CloseIn(BaseModel):
    """关单请求:service_id 可选(关单动作以工单状态推进为准)。

    ⚠️ ``service_id`` 仅 AUTH_MODE=internal 回归兜底;enforce 下无身份效力。
    """

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


def _scope_ticket(
    ticket: Ticket, identity: security.Identity, ident: security.Identity
) -> Ticket:
    """enforce 下坐席只能操作自己租户(merchant)的工单;internal 跳过。

    越权统一按 404 处理,避免泄露其他租户工单是否存在。
    """
    if not identity.is_internal and ticket.merchant_id != ident.merchant_id:
        raise NotFoundError(f"工单不存在: {ticket.id}")
    return ticket


def _publish_ticket_events(
    ticket: Ticket, *, claimed_by: str | None = None, closed_by: str | None = None
) -> None:
    """写库 commit 之后广播工单事件(ticket_status → buyer;ticket_update → queue)。

    buyer 频道需要 session_id,拿不到时跳过买家侧并保留 queue 侧广播。
    """
    if ticket.session_id:
        buyer_payload: dict[str, Any] = {
            "ticket_id": ticket.id,
            "session_id": ticket.session_id,
            "status": ticket.status,
        }
        if claimed_by:
            buyer_payload["claimed_by"] = claimed_by
        if closed_by:
            buyer_payload["closed_by"] = closed_by
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
    request: Request,
    merchant_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """工单队列:queued 优先 → 组内 last_msg_at DESC(NULL 最后)→ created_at DESC。

    enforce 下强制按 token 的 merchant_id 过滤(query 同名字段无身份效力);
    internal 保持旧行为。
    """
    ident = security.effective_identity(
        identity, "agent", fallback=dict(request.query_params)
    )
    stmt = select(Ticket)
    scope_merchant = merchant_id if identity.is_internal else ident.merchant_id
    if scope_merchant:
        stmt = stmt.where(Ticket.merchant_id == scope_merchant)
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
def ticket_detail(
    ticket_id: str,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """工单详情:工单结构 + 全量消息(created_at asc);enforce 下校验租户归属。"""
    ident = security.effective_identity(identity, "agent")
    ticket = _scope_ticket(_get_ticket(db, ticket_id), identity, ident)
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


@router.post(
    "/tickets/{ticket_id}/claim",
    dependencies=[Depends(security.require_replay_guard)],
)
def claim_ticket(
    ticket_id: str,
    payload: ClaimIn,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """守卫式领单:并发只有一个成功,被抢返回 409「已被他人领取」。"""
    ident = security.effective_identity(identity, "agent", payload=payload)
    # internal 回归兜底才读 body;enforce 下坐席身份只认 token,防止借 body 冒领。
    service_id = (
        (ident.service_id or payload.service_id)
        if identity.is_internal
        else ident.service_id
    )
    if not service_id:
        raise AppError(
            "VALIDATION_ERROR", "缺少坐席身份 service_id", status_code=400, param="service_id"
        )
    _scope_ticket(_get_ticket(db, ticket_id), identity, ident)
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
    "/tickets/{ticket_id}/messages",
    dependencies=[Depends(security.require_replay_guard)],
)
def add_ticket_message(
    ticket_id: str,
    payload: MessageIn,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """坐席回复:author_type=agent;关单后由服务守卫拒绝(409)。"""
    content = str(payload.content or "").strip()
    if not content:
        raise AppError(
            "VALIDATION_ERROR", "回复内容不能为空", status_code=400, param="content"
        )
    ident = security.effective_identity(identity, "agent", payload=payload)
    ticket = _scope_ticket(_get_ticket(db, ticket_id), identity, ident)
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
    # commit 已由 ticket_svc 完成;内部备注(buyer 不可见)不向买家频道广播。
    if not message.is_internal:
        if ticket.session_id:
            security.publish_event(
                security.channel_buyer(ticket.session_id),
                "agent_message",
                ticket_id=ticket.id,
                session_id=ticket.session_id,
                message_id=message.id,
                content=message.content,
                author_type="agent",
            )
        # 拿不到 session_id 的历史工单跳过买家侧,只在队列频道刷新。
    security.publish_event(
        security.channel_queue(ticket.merchant_id),
        "ticket_update",
        ticket_id=ticket.id,
        status=ticket.status,
    )
    return {"ok": True, "message": _message_dict(message)}


@router.post(
    "/tickets/{ticket_id}/close",
    dependencies=[Depends(security.require_replay_guard)],
)
def close_ticket(
    ticket_id: str,
    payload: CloseIn | None = None,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """关单:queued/claimed/waiting_customer → closed;重复关单 409。"""
    ident = security.effective_identity(identity, "agent", payload=payload)
    _scope_ticket(_get_ticket(db, ticket_id), identity, ident)
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
    body: dict[str, Any] = {"ok": True, "receipt": _receipt(ticket)}
    if closed_by:
        body["closed_by"] = closed_by
    # commit 已由 ticket_svc 完成。
    _publish_ticket_events(ticket, closed_by=closed_by)
    return body


# ---------------------------------------------------------------------------
# W7-4:Copilot 建议回复(规则模板,标 MOCK;不发起真实 LLM 调用)
# ---------------------------------------------------------------------------

#: MOCK:确定性规则模板版本串;本接口不调用任何真实模型,响应里显式标注。
_SUGGEST_MODEL = "rule-suggest-v1 (MOCK)"
#: 建议回复被禁词整句剔除后为空时的兜底话术(不承诺时效/金额)。
_SUGGEST_FALLBACK_REPLY = "已收到您的问题,我们会尽快核实处理,请以实际处理结果为准。"
#: 摘要被禁词剔除后为空时的兜底摘要(同样不含承诺)。
_SUGGEST_FALLBACK_SUMMARY = "买家诉求待坐席进一步核实。"
#: 摘要字数上限(任务书 W7-4:≤120 字)。
_SUGGEST_SUMMARY_MAX = 120
#: POST 生成结果的进程内缓存(按 ticket_id);GET 只读缓存,不产生副作用。
_SUGGEST_CACHE: dict[str, dict[str, Any]] = {}


def _suggest_user_texts(messages: list[TicketMessage]) -> list[str]:
    """取买家可见消息正文(created_at asc),作为生成建议的转录输入。"""
    return [
        message.content.strip()
        for message in messages
        if message.author_type == "user"
        and not message.is_internal
        and message.content.strip()
    ]


def _suggest_query(ticket: Ticket, user_texts: list[str]) -> str:
    """用最后 3 条买家消息 + 工单摘要拼检索 query(无消息时用摘要兜底)。"""
    parts = user_texts[-3:]
    if ticket.summary:
        parts.append(ticket.summary)
    return " ".join(parts).strip()


def _suggest_citations(
    db: Session, merchant_id: str, query: str, top_k: int = 3
) -> list[RetrievedChunk]:
    """从商户知识库重建 BM25 索引并检索真实条款;未覆盖时返回空列表(不编造)。"""
    if not query:
        return []
    rows = db.execute(
        select(KnowledgeItem).where(
            KnowledgeItem.merchant_id == merchant_id,
            KnowledgeItem.deleted_at.is_(None),
            KnowledgeItem.clause_id.is_not(None),
        )
    ).scalars().all()
    chunks = [
        PolicyChunk(
            clause_id=row.clause_id or "",
            version=row.version,
            title=row.title or "",
            text=row.content,
            effective_date="",
        )
        for row in rows
    ]
    backend = BM25Backend()
    backend.build(chunks)
    return backend.search(query, top_k=top_k, threshold=DEFAULT_THRESHOLD).results


def _compose_suggestion(
    ticket: Ticket, user_texts: list[str], citations: list[RetrievedChunk]
) -> dict[str, str]:
    """MOCK:规则模板生成 ``{summary, suggested_reply}``,不调用真实模型。

    确定性拼装:摘要 = 关键诉求 + 命中条款号;建议回复 = 致谢 + 条款依据 +
    处理中说明,模板本身不含时效/金额承诺;调用方仍统一过禁词剔除。
    monkeypatch 本函数即可测试禁词剔除分支(测试注入含承诺的草稿)。
    """
    demand = (user_texts[-1] if user_texts else (ticket.summary or "")).strip()
    demand = demand[:60] or "买家转人工咨询售后问题"
    clause_ids = "、".join(chunk.clause_id for chunk in citations[:2])
    if clause_ids:
        summary = f"买家诉求:{demand};命中条款:{clause_ids}"
    else:
        summary = f"买家诉求:{demand};政策未覆盖,需坐席人工核实"

    if clause_ids:
        body = f"经核对店铺相关政策条款({clause_ids}),我们会按政策为您跟进核实。"
    else:
        body = "您的诉求已记录,我们会尽快为您核实处理。"
    return {
        "summary": summary,
        "suggested_reply": (
            "您好,已收到您反馈的问题。" + body + "处理结果会通过本工单同步给您,请以实际处理结果为准。"
        ),
    }


@router.post("/tickets/{ticket_id}/suggest")
def suggest_reply(
    ticket_id: str,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """生成建议回复:工单转录 + 命中条款 → ``{summary, suggested_reply}``。

    - 规则模板 + MOCK 标注(``mock``/``model``),不发起真实 LLM 调用;
    - 摘要与建议回复都过 ``has_commitment_claim`` / ``strip_commitment_sentences``,
      剔除含到账时间/赔偿金额的整句;剔除后为空则给确定性兜底话术;
    - 工单不存在 404;已关闭工单按现有写接口风格返回 409(关单后无法再回复)。
    """
    ident = security.effective_identity(identity, "agent")
    ticket = _scope_ticket(_get_ticket(db, ticket_id), identity, ident)
    if ticket.status == "closed":
        raise StateConflictError(
            "工单已关闭,不能生成建议回复",
            ticket.status,
            param="status",
        )
    messages = list(
        db.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
        ).all()
    )
    user_texts = _suggest_user_texts(messages)
    citations = _suggest_citations(
        db, ticket.merchant_id, _suggest_query(ticket, user_texts)
    )
    draft = _compose_suggestion(ticket, user_texts, citations)

    summary = str(draft.get("summary") or "").strip()
    if has_commitment_claim(summary):
        summary = strip_commitment_sentences(summary)
    summary = summary[:_SUGGEST_SUMMARY_MAX] or _SUGGEST_FALLBACK_SUMMARY

    suggested_reply = str(draft.get("suggested_reply") or "").strip()
    if has_commitment_claim(suggested_reply):
        suggested_reply = strip_commitment_sentences(suggested_reply)
    suggested_reply = suggested_reply or _SUGGEST_FALLBACK_REPLY

    data: dict[str, Any] = {
        "ticket_id": ticket.id,
        "summary": summary,
        "suggested_reply": suggested_reply,
        "citations": [
            {
                "clause_id": chunk.clause_id,
                "version": chunk.version,
                "score": round(float(chunk.score), 4),
            }
            for chunk in citations
        ],
        "mock": True,
        "model": _SUGGEST_MODEL,
        "generated_at": _iso(utc_now()),
    }
    _SUGGEST_CACHE[ticket.id] = data
    return {"ok": True, "data": data}


@router.get("/tickets/{ticket_id}/suggest")
def get_suggestion(
    ticket_id: str,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """读取最近一次生成结果(进程内缓存按 ticket_id);无缓存返回 ``data=null``。

    GET 只读缓存、不写任何状态,因此重复调用不会改变后续 GET 的结果。
    """
    ident = security.effective_identity(identity, "agent")
    ticket = _scope_ticket(_get_ticket(db, ticket_id), identity, ident)
    return {"ok": True, "data": _SUGGEST_CACHE.get(ticket.id)}
