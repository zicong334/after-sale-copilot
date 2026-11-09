"""api/confirm.py — 确认卡闭环 API(W3-C)。

契约(任务书 W3-C + PRD §4 第 5 项 + G7 卡):

- ``GET  /api/c/confirm/pending?thread_id=...``:按 thread_id(=chat_session.id)从
  checkpointer 读当前挂起状态,返回待确认卡(四要素 + token + 服务端剩余秒数)。
- ``POST /api/c/confirm/{token}/confirm``:确认 → ``graph resume(True)`` → 图内
  ``_execute_confirmed`` 执行真实写工具 → **回读 DB** 组装 verified 回执。
- ``POST /api/c/confirm/{token}/reject``:拒绝 → ``graph resume(False)`` → 零写操作。
- 超时 10 分钟(服务端时间)= 拒绝 + 留草稿:读取/确认/拒绝任一入口发现
  ``expires_at <= now`` 时,一律走拒绝分支落终态,并把确认卡标记 ``expired``;
  payload/四要素(草稿)保留在 ``confirm_request`` 行里,回执里也带一份。

幂等:token 对应卡已 confirmed 时重复 POST 直接回放同一业务单回执(不重复执行);
已 cancelled 时重复拒绝回放拒绝结果;已 cancelled 后再确认 → 409。
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import AppError, NotFoundError, StateConflictError
from app.graph import build as graph_build
from app.graph import runner
from app.models import ChatMessage, ChatSession, RefundRequest, Ticket
from app.tools.events import ConfirmRequest, get_confirm

router = APIRouter(prefix="/api/c/confirm", tags=["confirm"])

_TOOL_REFUND = "refund__create"
_TOOL_HANDOFF = "handoff__create_ticket"

#: W6-A1:按确认 token 串行化"确认"入口(进程内)。
#:
#: 10 个线程同时点同一张卡时,若各自"先读状态再 resume",会并行进入图执行分支,
#: 造成同一确认卡被多次执行(副作用重复)。这里用**每 token 一把锁**把"读状态 →
#: 判定 → resume"变成临界区:第一个赢家把卡推进到 confirmed,其余线程在锁内重读
#: 后直接走幂等回执。图内 ``_execute_confirmed`` 另有条件更新(CAS)做跨进程纵深。
_CONFIRM_LOCKS: dict[str, threading.Lock] = {}
_CONFIRM_LOCKS_GUARD = threading.Lock()


@contextmanager
def _confirm_lock(token: str):
    """取该 token 的互斥锁(惰性创建;锁表本身用一把小锁保护)。"""
    with _CONFIRM_LOCKS_GUARD:
        lock = _CONFIRM_LOCKS.setdefault(str(token), threading.Lock())
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


class ThreadRef(BaseModel):
    """确认/拒绝请求体:只带会话线程 id(会话归属校验在服务端完成)。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str


def _now() -> datetime:
    """可注入的服务端时钟:测试用假时钟证明 10 分钟超时路径。"""
    return utc_now()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def _read_state(thread_id: str) -> dict[str, Any]:
    """直接从 checkpointer 读该线程最新状态(确认卡/执行结果/草稿都在这)。"""
    saver = graph_build.open_checkpointer(graph_build.DEFAULT_CHECKPOINT_PATH)
    try:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if tup is None:
            return {}
        return dict((tup.checkpoint or {}).get("channel_values") or {})
    finally:
        saver.conn.close()


def _require_session(db: Session, thread_id: str) -> ChatSession:
    row = db.get(ChatSession, thread_id)
    if row is None:
        raise NotFoundError(f"会话不存在: {thread_id}")
    return row


def _status_of(db: Session, thread_id: str) -> str:
    return str(_require_session(db, thread_id).status or "active")


def _require_confirm(db: Session, thread_id: str, token: str) -> tuple[dict[str, Any], ConfirmRequest]:
    """取该 thread 的确认状态 + 卡行,并校验 token 确实属于该 thread。"""
    _require_session(db, thread_id)
    state = _read_state(thread_id)
    confirm = state.get("confirm") or {}
    if str(confirm.get("token") or "") != token:
        raise AppError(
            "CONFIRM_THREAD_MISMATCH",
            "确认卡不属于该会话(thread_id 与 token 不匹配)",
            status_code=409,
        )
    request = get_confirm(db, token)
    if request is None:
        raise AppError(
            "CONFIRM_NOT_FOUND", "确认卡不存在或已被清理", status_code=404
        )
    return state, request


def _card_of(confirm: dict[str, Any]) -> dict[str, str]:
    card = dict(confirm.get("card") or {})
    return {key: str(card.get(key) or "") for key in ("what", "impact", "irreversibility", "alternatives")}


def _args_of(request: ConfirmRequest) -> dict[str, Any]:
    try:
        return dict(json.loads(request.payload).get("args") or {})
    except (TypeError, ValueError):  # pragma: no cover - payload 由本仓写入
        return {}


def _draft(confirm: dict[str, Any], request: ConfirmRequest) -> dict[str, Any]:
    """超时/拒绝后保留的草稿:四要素 + 待执行参数 + 卡状态。"""
    return {
        "confirm_token": request.confirm_token,
        "tool": request.tool_name,
        "card": _card_of(confirm),
        "args": _args_of(request),
        "status": request.status,
        "expires_at_utc": _iso(request.expires_at),
    }


def _expired(request: ConfirmRequest) -> bool:
    expires_at = _as_utc(request.expires_at)
    return expires_at is not None and expires_at <= _now()


def _seconds_left(request: ConfirmRequest) -> int:
    expires_at = _as_utc(request.expires_at)
    if expires_at is None:
        return 0
    return max(0, int((expires_at - _now()).total_seconds()))


def _pending_body(
    thread_id: str, state: dict[str, Any], confirm: dict[str, Any], request: ConfirmRequest
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "pending",
        "thread_id": thread_id,
        "confirm_token": request.confirm_token,
        "tool": request.tool_name,
        "card": _card_of(confirm),
        "expires_at_utc": _iso(request.expires_at),
        "expires_in_seconds": _seconds_left(request),
        "trace_id": str(state.get("trace_id") or ""),
        "draft": _draft(confirm, request),
    }


def _append_assistant(db: Session, thread_id: str, reply: str, trace_id: str, intent: str) -> None:
    """确认后的终态回复补一条 assistant 消息,会话摘要有据可查。"""
    if not reply:
        return
    db.add(
        ChatMessage(
            session_id=thread_id,
            role="assistant",
            content=reply,
            attachments=[],
            trace_id=trace_id,
            intent=intent,
        )
    )
    db.commit()


def _timeout(
    db: Session, thread_id: str, state: dict[str, Any], confirm: dict[str, Any], request: ConfirmRequest
) -> dict[str, Any]:
    """超时 = 拒绝:图落终态(不执行写工具),卡标记 expired,草稿保留。"""
    result = runner.resume(thread_id, False, session=db)
    request = get_confirm(db, request.confirm_token) or request
    if request.status == "pending":
        request.status = "expired"
        db.commit()
    trace_id = str(state.get("trace_id") or "")
    _append_assistant(db, thread_id, result.reply, trace_id, "confirm_timeout")
    return {
        "ok": True,
        "status": "expired",
        "decision": "rejected",
        "timeout": True,
        "thread_id": thread_id,
        "confirm_token": request.confirm_token,
        "tool": request.tool_name,
        "card": _card_of(confirm),
        "draft": _draft(confirm, request),
        "reply": result.reply,
        "session_status": result.session_status,
        "trace_id": trace_id,
    }


def _receipt(
    db: Session, state: dict[str, Any], request: ConfirmRequest, *, replay: bool = False
) -> dict[str, Any]:
    """回执:业务单号 + DB 回读 status(verified)+ UTC 时间 + trace_id。

    ``replay=True`` 表示"本次是幂等回放"——业务单早先已创建,回执必须如实标注
    ``idempotent_hit=True``,不能把回放当成新执行(W6-A1/A8)。
    """
    confirm = state.get("confirm") or {}
    executed = dict(confirm.get("executed") or {})
    data = dict(executed.get("data") or {})
    tool = str(confirm.get("tool") or request.tool_name)
    trace_id = str(state.get("trace_id") or "")
    if tool == _TOOL_REFUND:
        refund = db.get(RefundRequest, str(data.get("refund_id") or ""))
        if refund is None:  # pragma: no cover - 图内返回 ok 时必然已落库
            return {"kind": "refund", "verified": False, "status": "unknown", "trace_id": trace_id}
        return {
            "kind": "refund",
            "refund_request_id": refund.id,
            "order_id": refund.order_id,
            "amount_cents": refund.amount_cents,
            "status": refund.status,
            "occurred_at_utc": _iso(refund.created_at),
            "trace_id": trace_id,
            "verified": refund.status == str(data.get("status") or ""),
            "idempotent_hit": True if replay else bool(data.get("idempotent_hit")),
        }
    ticket = db.get(Ticket, str(data.get("ticket_id") or ""))
    if ticket is None:  # pragma: no cover - 图内返回 ok 时必然已落库
        return {"kind": "handoff", "verified": False, "status": "unknown", "trace_id": trace_id}
    return {
        "kind": "handoff",
        "ticket_id": ticket.id,
        "order_id": ticket.order_id,
        "status": ticket.status,
        "occurred_at_utc": _iso(ticket.created_at),
        "trace_id": trace_id,
        "verified": ticket.status == str(data.get("status") or ""),
        "idempotent_hit": bool(replay),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.get("/pending")
def pending(thread_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """待确认卡(按 thread_id 读 checkpointer);已超时则在此落实"超时=拒绝"。"""
    _require_session(db, thread_id)
    state = _read_state(thread_id)
    confirm = state.get("confirm") or {}
    token = str(confirm.get("token") or "")
    if not token or confirm.get("decided"):
        raise AppError(
            "CONFIRM_NOT_PENDING", "当前会话没有待确认的操作", status_code=404
        )
    request = get_confirm(db, token)
    if request is None:
        raise AppError("CONFIRM_NOT_FOUND", "确认卡不存在或已被清理", status_code=404)
    if _expired(request):
        return _timeout(db, thread_id, state, confirm, request)
    return _pending_body(thread_id, state, confirm, request)


@router.post("/{confirm_token}/confirm")
def confirm(
    confirm_token: str, payload: ThreadRef, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """人工确认 → graph resume → 真实写操作 → verified 回执(幂等,不重复执行)。

    W6-A1:整段"读状态 → 判定 → resume"在**每 token 一把锁**内执行,锁内先
    ``expire_all`` 再重读,保证并发点击时只有一个请求真正执行写工具,其余拿到
    幂等回执(``idempotent=True``、``receipt.idempotent_hit=True``)。
    """
    with _confirm_lock(confirm_token):
        db.expire_all()
        state, request = _require_confirm(db, payload.thread_id, confirm_token)
        confirm_state = state.get("confirm") or {}
        if request.status == "confirmed":
            receipt = _receipt(db, state, request, replay=True)
            return {
                "ok": True,
                "decision": "confirmed",
                "idempotent": True,
                "thread_id": payload.thread_id,
                "receipt": receipt,
                "reply": str(state.get("draft") or ""),
                "session_status": _status_of(db, payload.thread_id),
                "trace_id": str(state.get("trace_id") or ""),
            }
        if request.status == "cancelled":
            raise StateConflictError("确认卡已被拒绝,不能再次确认", request.status, param="status")
        if request.status == "expired" or _expired(request):
            return _timeout(db, payload.thread_id, state, confirm_state, request)

        result = runner.resume(payload.thread_id, True, session=db)
        request = get_confirm(db, confirm_token) or request
        if request.status != "confirmed":
            # 执行失败(业务拒绝/结果未知):不谎报成功,回执 verified=false。
            executed = dict((_read_state(payload.thread_id).get("confirm") or {}).get("executed") or {})
            return {
                "ok": False,
                "decision": "failed",
                "thread_id": payload.thread_id,
                "error": dict(executed.get("error") or {}),
                "reply": result.reply,
                "session_status": result.session_status,
                "trace_id": str(state.get("trace_id") or ""),
            }
        state = _read_state(payload.thread_id)
        receipt = _receipt(db, state, request)
        trace_id = str(state.get("trace_id") or "")
        _append_assistant(db, payload.thread_id, result.reply, trace_id, "confirm")
        return {
            "ok": True,
            "decision": "confirmed",
            "idempotent": False,
            "thread_id": payload.thread_id,
            "receipt": receipt,
            "reply": result.reply,
            "session_status": result.session_status,
            "trace_id": trace_id,
        }


@router.post("/{confirm_token}/reject")
def reject(
    confirm_token: str, payload: ThreadRef, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """人工拒绝 → graph resume(False) → 零写操作;草稿保留。

    W7-6:与 confirm 共用**每 token 一把锁**(``_confirm_lock``)。整段"读状态 →
    判定 → resume → 置 cancelled"在锁内执行,锁内先 ``expire_all`` 再重读:
    极端并发下第二次 reject 读到的是已 cancelled 的终态,直接返回幂等回执
    (``idempotent=True``,``cancelled`` 已回放),不会双写/409。
    """
    with _confirm_lock(confirm_token):
        db.expire_all()
        state, request = _require_confirm(db, payload.thread_id, confirm_token)
        confirm_state = state.get("confirm") or {}
        if request.status == "cancelled":
            return {
                "ok": True,
                "decision": "rejected",
                "idempotent": True,
                "thread_id": payload.thread_id,
                "receipt": None,
                "draft": _draft(confirm_state, request),
                "reply": str(state.get("draft") or ""),
                "session_status": _status_of(db, payload.thread_id),
                "trace_id": str(state.get("trace_id") or ""),
            }
        if request.status == "confirmed":
            raise StateConflictError("确认卡已执行,不能拒绝", request.status, param="status")
        if request.status == "expired" or _expired(request):
            return _timeout(db, payload.thread_id, state, confirm_state, request)

        result = runner.resume(payload.thread_id, False, session=db)
        request = get_confirm(db, confirm_token) or request
        if request.status == "pending":
            request.status = "cancelled"
            db.commit()
        trace_id = str(state.get("trace_id") or "")
        _append_assistant(db, payload.thread_id, result.reply, trace_id, "confirm_reject")
        return {
            "ok": True,
            "decision": "rejected",
            "idempotent": False,
            "thread_id": payload.thread_id,
            "receipt": None,
            "draft": _draft(confirm_state, request),
            "reply": result.reply,
            "session_status": result.session_status,
            "trace_id": trace_id,
        }
