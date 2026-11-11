"""api/badcases.py — W12-2 数据飞轮:工作台「标记坏例」写入口。

路由一览(prefix ``/api/b``):

- ``POST /api/b/badcases``:打包一份坏例快照(trace_id + 转录 + 工具链 +
  judge + 坐席修改 diff)写入 ``badcase`` 表,返回 ``badcase_id``。

设计要点
--------
- **只读现有业务表**:转录复用工作台取数口径(``app.api.agent`` 的
  ``_chat_transcript`` / ``_ticket_messages``),工具链读 ``tool_events``,
  建议原文优先取进程内 ``_SUGGEST_CACHE``(服务端真值),前端回传的展示文本
  只作兜底;不重写任何状态机,也不改工单。
- **real 工单默认没有离线裁判**:``judge_score`` 仅在调用方显式注入时写入;
  缺失时 ``judge_detail`` 记录 ``{"source": "unavailable", ...}``,不伪造分数。
- **不碰 ``data/eval/cases_v1.json``**:本路由只落库;草稿由
  ``scripts/promote_badcase.py`` 生成到 ``data/eval/drafts/``,入集走主会话治理。
"""
from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.api import agent as agent_api
from app.db import utc_now
from app.models import BadCase, ChatMessage
from app.tools.events import ToolEvent

router = APIRouter(prefix="/api/b", tags=["badcases"])

#: 单条字段落库上限,防止把整篇长文塞进审计表。
_TRANSCRIPT_MAX_ENTRIES = 200
_TOOL_CHAIN_MAX_ENTRIES = 200
_ARGS_JSON_MAX = 1000
_NOTE_MAX = 2000
_TEXT_MAX = 8000


def _iso(value: datetime | None) -> str | None:
    """datetime → UTC ISO8601 字符串(与工作台其它投影保持同一口径)。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_json_text(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _clip(value: Any) -> Any:
    """把工具参数裁到可读长度(仍是 JSON,便于草稿/回执查看)。"""
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > _ARGS_JSON_MAX:
        return json.loads(json.dumps(text[:_ARGS_JSON_MAX] + "…", ensure_ascii=False))
    return value


def _transcript(db: Session, ticket: Any) -> list[dict[str, Any]]:
    """转人工前聊天 + 工单消息,按时间顺序拼成一份可复现转录。"""
    rows: list[dict[str, Any]] = []
    for item in agent_api._chat_transcript(db, ticket):
        rows.append(
            {
                "role": item.get("author_type") or "user",
                "content": item.get("content") or "",
                "at": item.get("created_at_utc"),
                "source": "chat",
                "internal": False,
            }
        )
    for message in agent_api._ticket_messages(db, ticket.id):
        rows.append(
            {
                "role": message.author_type,
                "content": message.content,
                "at": _iso(message.created_at),
                "source": "ticket",
                "internal": bool(message.is_internal),
            }
        )
    return rows[:_TRANSCRIPT_MAX_ENTRIES]


def _trace_ids(db: Session, ticket: Any, extra: str | None) -> list[str]:
    """工单会话下的全部 trace_id(前端传入的单轮 trace 作为兜底追加)。"""
    ids: list[str] = []
    if ticket.session_id:
        rows = db.scalars(
            select(ChatMessage.trace_id)
            .where(
                ChatMessage.session_id == ticket.session_id,
                ChatMessage.trace_id.is_not(None),
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        ).all()
        for value in rows:
            text = (value or "").strip()
            if text and text not in ids:
                ids.append(text)
    if extra:
        text = extra.strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def _tool_chain(db: Session, trace_ids: list[str]) -> list[dict[str, Any]]:
    """该 trace 下的真实工具调用(排除编排事件:payload 里带 event 键的那些)。"""
    if not trace_ids:
        return []
    events = db.scalars(
        select(ToolEvent)
        .where(ToolEvent.trace_id.in_(trace_ids))
        .order_by(ToolEvent.ts.asc(), ToolEvent.step.asc())
    ).all()
    chain: list[dict[str, Any]] = []
    for event in events:
        payload = _parse_json_text(event.payload)
        if isinstance(payload, dict) and "event" in payload:
            # router_failover / graph__prompt_probe_block 等编排事件不是工具调用。
            continue
        args = payload.get("args") if isinstance(payload, dict) else None
        chain.append(
            {
                "tool": event.tool_name,
                "ok": bool(event.ok),
                "error_code": event.error_code,
                "step": event.step,
                "args": _clip(args),
            }
        )
        if len(chain) >= _TOOL_CHAIN_MAX_ENTRIES:
            break
    return chain


def _agent_final(db: Session, ticket_id: str) -> str | None:
    """坐席最后一条**公开发送**的回复(内部备忘记入转录但不当作最终对客行为)。"""
    messages = agent_api._ticket_messages(db, ticket_id)
    for message in reversed(messages):
        if message.author_type == "agent" and not message.is_internal:
            return message.content
    return None


def _suggestion(ticket_id: str, provided: str | None) -> str | None:
    """建议原文:优先服务端缓存(真值),前端展示文本兜底。"""
    if provided and provided.strip():
        return provided.strip()
    cached = agent_api._SUGGEST_CACHE.get(ticket_id) or {}
    text = cached.get("suggested_reply")
    return str(text).strip() if text else None


def _diff(suggestion: str | None, final: str | None) -> str | None:
    """坐席修改 diff(AI 建议 → 坐席最终回复);任一侧缺失则不生成。"""
    if not suggestion or not final or suggestion == final:
        return None
    lines = difflib.unified_diff(
        suggestion.splitlines(),
        final.splitlines(),
        fromfile="ai_suggestion",
        tofile="agent_final",
        lineterm="",
    )
    return "\n".join(lines) or None


class JudgeIn(BaseModel):
    """judge 分(可选):实时工单不跑离线裁判,由评测/人工裁决注入。"""

    score: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: dict[str, Any] | None = None


class BadCaseIn(BaseModel):
    """标记坏例请求体。"""

    ticket_id: str = Field(min_length=1, max_length=64)
    service_id: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)
    note: str = Field(default="", max_length=_NOTE_MAX)
    suggestion: str | None = Field(default=None, max_length=_TEXT_MAX)
    judge: JudgeIn | None = None


def _judge_fields(payload: BadCaseIn) -> tuple[float | None, dict[str, Any]]:
    if payload.judge is not None:
        detail: dict[str, Any] = dict(payload.judge.detail or {})
        detail.setdefault("source", "caller")
        return payload.judge.score, detail
    return None, {
        "source": "unavailable",
        "reason": "实时工单不跑离线裁判:judge 分需评测/人工裁决注入,本行不伪造分数",
    }


@router.post("/badcases", status_code=status.HTTP_201_CREATED)
def create_badcase(
    payload: BadCaseIn,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """标记坏例:打包现场快照写 ``badcase`` 表(不改工单、不调模型)。"""
    ident = security.effective_identity(identity, "agent", payload=payload)
    ticket = agent_api._scope_ticket(
        agent_api._get_ticket(db, payload.ticket_id), identity, ident
    )

    transcript = _transcript(db, ticket)
    trace_ids = _trace_ids(db, ticket, payload.trace_id)
    tool_chain = _tool_chain(db, trace_ids)
    suggestion = _suggestion(ticket.id, payload.suggestion)
    agent_final = _agent_final(db, ticket.id)
    diff = _diff(suggestion, agent_final)
    judge_score, judge_detail = _judge_fields(payload)

    row = BadCase(
        ticket_id=ticket.id,
        merchant_id=ticket.merchant_id,
        order_id=ticket.order_id,
        handoff_reason=ticket.handoff_reason,
        trace_id=trace_ids[-1] if trace_ids else None,
        service_id=(payload.service_id or ident.service_id or None),
        suggestion=suggestion,
        agent_final=agent_final,
        diff=diff,
        transcript=json.dumps(transcript, ensure_ascii=False),
        tool_chain=json.dumps(tool_chain, ensure_ascii=False),
        judge_score=judge_score,
        judge_detail=json.dumps(judge_detail, ensure_ascii=False),
        note=(payload.note or "").strip(),
        status="new",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "ok": True,
        "data": {
            "badcase_id": row.id,
            "ticket_id": row.ticket_id,
            "trace_ids": trace_ids,
            "transcript_entries": len(transcript),
            "tool_chain_entries": len(tool_chain),
            "has_diff": diff is not None,
            "judge_score": row.judge_score,
            "judge_source": judge_detail.get("source"),
            "status": row.status,
            "created_at_utc": _iso(row.created_at) or _iso(utc_now()),
        },
    }
