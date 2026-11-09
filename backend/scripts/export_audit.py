"""export_audit.py — 单案审计链导出(W7-5)。

把一条 trace(感知→分类→检索→工具→确认→执行→回执)或一张退款单的相关审计
数据,从库里回读成一个 JSON 对象打印到 stdout,供客服主管/审计/复盘使用。

用法(在 backend 目录下运行)::

    cd backend
    ../.venv/Scripts/python scripts/export_audit.py --trace-id <trace_id>
    ../.venv/Scripts/python scripts/export_audit.py --refund-id <refund_id>
    ../.venv/Scripts/python scripts/export_audit.py --trace-id <trace_id> \
        --db ../.tmp/case.db --pretty

参数:
- ``--trace-id`` / ``--refund-id`` 二选一(必填),支持双向定位:
  给定 trace 找退款单;给定退款单反查 trace。
- ``--db`` 可选 SQLite 路径;缺省沿用项目 ``app.db.get_database_url()``
  (默认 backend/dev.db,可用 DATABASE_URL 覆盖)。
- ``--pretty`` 可选:缩进 2 空格输出,便于人读。

退出码:0 = 成功,stdout 恰好一个 JSON 对象;2 = 找不到记录/数据库文件缺失,
错误只写 stderr(stdout 保持干净,便于管道解析)。

字段口径(不编造、缺失即 null):
- ``percepts``:感知结果(``Percept``)只存在于图状态、不落库,故用该 trace 的
  user ``chat_message`` 原文经 ``app.perception.normalize`` 确定性重放(纯函数、
  无 IO),字段与图内一致。
- ``citations``:引用条款结果同样不落库。分两条确定性回读路径:
  ① ``policy__check_return`` 事件:用既有入参重放纯函数 ``_check_return`` 得
  clause_id(与 ``app/tools/refund.py`` 策略门同一做法),再按 clause_id 从
  ``knowledge_item`` 回读 version;
  ② ``policy__search`` 事件:用既有入参在 ``knowledge_item`` 上重建 BM25、
  按同一阈值检索并取前 3 条(与图内 retrieve 阶段一致)。
  查不到 ``knowledge_item`` 的条款直接跳过(不硬凑版本号)。
- ``confirm``:``confirm_request`` 表没有 trace_id、也没有状态迁移历史,故
  ① 已执行 trace 用 ``graph__confirm_executed.payload.confirm_token`` 强关联;
  ② 未执行 trace 用"写工具事件的 args"与确认卡 ``payload.args`` 一致性配对
  (取事件之后创建的最早一张同工具卡);
  ③ 迁移时间由 ``created_at``(pending)与执行事件时间(confirmed)推导;
  cancelled/expired 无时间戳留存量,``at`` 显式 null。
- ``refund``:``refund_request`` 表没有 trace_id,正向定位先按执行事件的
  ``payload.result_id``(=refund_id)回读,缺失时用 ``refund__create`` 事件入参
  的 (order_id, amount_cents, reason) 兜底匹配;反向定位先扫执行事件
  ``result_id``,再退到同样的三要素匹配。
- ``receipt.verified``:表示"执行事件回读指向该业务单"(表内无执行结果快照,
  不重算 status 一致性);``idempotent_hit`` 直接取执行事件回读值,无事件则 null。
- ``integrity``:七段(percept/classify/retrieve/tool/confirm/execute/receipt)
  逐段判定是否有真实落库痕迹,缺哪段写进 ``missing_stages``。

本脚本只读:不写任何业务表、不产生新事件、不调用 LLM/网络。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 允许 `cd backend && ../.venv/Scripts/python scripts/export_audit.py` 直接运行:
# 把 backend/ 插到 sys.path 最前,后续才能 import app.*(与既有 scripts 一致)。
ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402 - 必须在上面的 sys.path 处理之后导入
from sqlalchemy.orm import Session  # noqa: E402

from app.db import create_db_engine, get_database_url, utc_now  # noqa: E402
from app.graph.nodes import (  # noqa: E402
    EVENT_GRAPH_CONFIRM_EXECUTED,
    EVENT_GRAPH_PROMPT_BLOCK,
    EVENT_GRAPH_STEP_LIMIT,
)
from app.models import (  # noqa: E402
    ChatMessage,
    ChatSession,
    KnowledgeItem,
    RefundRequest,
    Ticket,
)
from app.perception import normalize  # noqa: E402
from app.retrieval.bm25_backend import BM25Backend, DEFAULT_THRESHOLD  # noqa: E402
from app.retrieval.chunker import PolicyChunk  # noqa: E402
from app.tools.events import ConfirmRequest, ToolEvent  # noqa: E402
from app.tools.handoff import TOOL_NAME as HANDOFF_TOOL_NAME  # noqa: E402
from app.tools.policy import (  # noqa: E402
    CHECK_TOOL_NAME,
    SEARCH_TOOL_NAME,
    _check_return,
)
from app.tools.refund import TOOL_NAME as REFUND_TOOL_NAME  # noqa: E402
from app.trace import EVENT_CLASSIFY, EVENT_QUERY_REWRITE, trace_events  # noqa: E402

#: 七段链路(顺序即完整性检查顺序)
STAGE_ORDER: tuple[str, ...] = (
    "percept",
    "classify",
    "retrieve",
    "tool",
    "confirm",
    "execute",
    "receipt",
)

#: 编排事件 → 阶段映射(未列出的编排事件归入 act)
_EVENT_STAGE: dict[str, str] = {
    EVENT_CLASSIFY: "classify",
    EVENT_QUERY_REWRITE: "retrieve",
    EVENT_GRAPH_PROMPT_BLOCK: "percept",
    EVENT_GRAPH_STEP_LIMIT: "act",
    EVENT_GRAPH_CONFIRM_EXECUTED: "execute",
}

#: 检索类工具事件 → retrieve 阶段;写工具事件 → confirm 阶段(handler 生成确认卡)
_RETRIEVE_TOOLS = (SEARCH_TOOL_NAME, CHECK_TOOL_NAME)
_WRITE_TOOLS = (REFUND_TOOL_NAME, HANDOFF_TOOL_NAME)


class AuditNotFound(RuntimeError):
    """审计对象不存在(脚本据此返回非 0 并只写 stderr)。"""


# ---------------------------------------------------------------------------
# 基础转换工具
# ---------------------------------------------------------------------------


def _as_utc(value: datetime | None) -> datetime | None:
    """统一成 UTC aware 时间;SQLite 回读的时间常常是 naive,按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """ISO8601 UTC 字符串;缺失返回 None(显式 null,不用空串)。"""
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def _parse_payload(raw: str | None) -> dict[str, Any]:
    """tool_events.payload 是 TEXT JSON:解析失败按空对象处理,不影响导出。"""
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _split_events(events: list[ToolEvent]) -> tuple[
    list[tuple[ToolEvent, dict[str, Any]]],
    list[tuple[ToolEvent, dict[str, Any]]],
]:
    """把事件分成(编排事件, 工具调用事件)。

    ``app.trace.record_event`` 写的事件 payload 一定带 ``payload["event"]``;
    工具调用事件的 payload 是 ``{"args": ...}``(写工具另带 pending_confirm)。
    """
    orchestration: list[tuple[ToolEvent, dict[str, Any]]] = []
    tool_calls: list[tuple[ToolEvent, dict[str, Any]]] = []
    for event in events:
        payload = _parse_payload(event.payload)
        if "event" in payload:
            orchestration.append((event, payload))
        else:
            tool_calls.append((event, payload))
    return orchestration, tool_calls


# ---------------------------------------------------------------------------
# 定位:refund ↔ trace / confirm / ticket
# ---------------------------------------------------------------------------


def _execute_event(
    orchestration: list[tuple[ToolEvent, dict[str, Any]]],
) -> tuple[ToolEvent | None, dict[str, Any]]:
    """取最后一条"确认门已执行"事件(一次副作用至多一条,取最后是兜底)。"""
    for event, payload in reversed(orchestration):
        if payload.get("event") == EVENT_GRAPH_CONFIRM_EXECUTED:
            return event, payload
    return None, {}


def _trace_of_refund(db: Session, refund: RefundRequest) -> str | None:
    """退款单 → trace:先扫执行事件 result_id,再用退款三要素兜底。"""
    executed = db.scalars(
        select(ToolEvent)
        .where(ToolEvent.tool_name == EVENT_GRAPH_CONFIRM_EXECUTED)
        .order_by(ToolEvent.ts.desc())
    ).all()
    for event in executed:
        payload = _parse_payload(event.payload)
        if str(payload.get("result_id") or "") == refund.id:
            return str(event.trace_id)
    created = db.scalars(
        select(ToolEvent)
        .where(ToolEvent.tool_name == REFUND_TOOL_NAME)
        .order_by(ToolEvent.ts.desc())
    ).all()
    for event in created:
        args = _parse_payload(event.payload).get("args") or {}
        if (
            str(args.get("order_id") or "") == str(refund.order_id or "")
            and args.get("amount_cents") is not None
            and int(args.get("amount_cents")) == int(refund.amount_cents)
            and str(args.get("reason") or "") == str(refund.reason or "")
        ):
            return str(event.trace_id)
    return None


def _refund_of_trace(
    db: Session, events: list[ToolEvent]
) -> RefundRequest | None:
    """trace → 退款单:先按执行事件 result_id 回读,再用三要素兜底。"""
    orchestration, tool_calls = _split_events(events)
    _, execute_payload = _execute_event(orchestration)
    if execute_payload.get("tool") == REFUND_TOOL_NAME:
        refund_id = str(execute_payload.get("result_id") or "")
        if refund_id:
            refund = db.get(RefundRequest, refund_id)
            if refund is not None:
                return refund
    for event, payload in reversed(tool_calls):
        if event.tool_name != REFUND_TOOL_NAME or not event.ok:
            continue
        args = payload.get("args") or {}
        order_id = str(args.get("order_id") or "")
        amount = args.get("amount_cents")
        reason = str(args.get("reason") or "")
        if not order_id or amount is None:
            continue
        return db.scalars(
            select(RefundRequest)
            .where(
                RefundRequest.order_id == order_id,
                RefundRequest.amount_cents == int(amount),
                RefundRequest.reason == reason,
            )
            .order_by(RefundRequest.created_at.desc())
        ).first()
    return None


def _ticket_of_trace(
    db: Session, orchestration: list[tuple[ToolEvent, dict[str, Any]]]
) -> Ticket | None:
    """转人工 trace → 工单(执行事件 result_id = ticket_id)。"""
    _, execute_payload = _execute_event(orchestration)
    if execute_payload.get("tool") != HANDOFF_TOOL_NAME:
        return None
    ticket_id = str(execute_payload.get("result_id") or "")
    return db.get(Ticket, ticket_id) if ticket_id else None


def _find_confirm(
    db: Session,
    orchestration: list[tuple[ToolEvent, dict[str, Any]]],
    tool_calls: list[tuple[ToolEvent, dict[str, Any]]],
) -> ConfirmRequest | None:
    """定位本 trace 的确认卡:执行事件 token 强关联 → 写工具入参兜底配对。"""
    for event, payload in orchestration:
        if payload.get("event") != EVENT_GRAPH_CONFIRM_EXECUTED:
            continue
        token = str(payload.get("confirm_token") or "")
        if not token:
            continue
        request = db.scalars(
            select(ConfirmRequest).where(ConfirmRequest.confirm_token == token)
        ).first()
        if request is not None:
            return request
    # 兜底:未执行的 trace 没有 token 事件,只能用"工具入参 = 确认卡草稿入参"配对。
    # 注意:handler 先落确认卡、registry 后写工具事件,故确认卡时间略早于事件时间;
    # 取"入参一致且时间最接近事件"的那张卡(同一 order 的幂等卡不会同时存在多张)。
    for event, payload in tool_calls:
        if not payload.get("pending_confirm"):
            continue
        args = payload.get("args") or {}
        if not args:
            continue
        call_at = _as_utc(event.ts)
        candidates = db.scalars(
            select(ConfirmRequest).where(ConfirmRequest.tool_name == event.tool_name)
        ).all()
        matched: list[tuple[float, ConfirmRequest]] = []
        for request in candidates:
            card_args = _parse_payload(request.payload).get("args") or {}
            if not all(card_args.get(key) == value for key, value in args.items()):
                continue
            card_at = _as_utc(request.created_at)
            if call_at is None or card_at is None:
                distance = 0.0
            else:
                distance = abs((card_at - call_at).total_seconds())
            matched.append((distance, request))
        if matched:
            matched.sort(key=lambda pair: pair[0])
            return matched[0][1]
    return None


# ---------------------------------------------------------------------------
# 引用条款回读(确定性重放 + 知识库回读)
# ---------------------------------------------------------------------------


def _clause_version(
    db: Session, clause_id: str, merchant_id: str | None
) -> str | None:
    """按 clause_id 回读 knowledge_item 的版本号;查不到返回 None。"""
    stmt = select(KnowledgeItem).where(
        KnowledgeItem.clause_id == str(clause_id),
        KnowledgeItem.deleted_at.is_(None),
    )
    if merchant_id:
        stmt = stmt.where(KnowledgeItem.merchant_id == merchant_id)
    row = db.scalars(stmt).first()
    return None if row is None else row.version


def _replay_policy_search(
    db: Session, merchant_id: str | None, query: str, top_k: int
) -> list[tuple[str, str]]:
    """按 policy__search 事件入参在 knowledge_item 上确定性重放检索(取前 3)。"""
    if not merchant_id or not query:
        return []
    rows = (
        db.execute(
            KnowledgeItem.__table__.select().where(
                KnowledgeItem.merchant_id == merchant_id,
                KnowledgeItem.deleted_at.is_(None),
                KnowledgeItem.clause_id.is_not(None),
            )
        )
        .mappings()
        .all()
    )
    chunks = [
        PolicyChunk(
            clause_id=row["clause_id"],
            version=row["version"],
            title=row["title"] or "",
            text=row["content"],
            effective_date="",
        )
        for row in rows
    ]
    if not chunks:
        return []
    backend = BM25Backend()
    backend.build(chunks)
    try:
        result = backend.search(query, top_k=top_k, threshold=DEFAULT_THRESHOLD)
    except (TypeError, ValueError, KeyError):
        return []
    # 与图内 retrieve 阶段一致:命中取前 3 条
    return [(item.clause_id, item.version) for item in result.results][:3]


def _collect_citations(
    db: Session,
    tool_calls: list[tuple[ToolEvent, dict[str, Any]]],
    merchant_id: str | None,
) -> list[dict[str, Any]]:
    """回读本 trace 的引用条款(去重,保持首次出现顺序)。"""
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(clause_id: str | None, version: str | None) -> None:
        if not clause_id or not version:
            return  # 知识库查不到版本就不引用,不硬凑
        if clause_id in seen:
            return
        seen.add(clause_id)
        citations.append({"clause_id": clause_id, "version": version})

    for event, payload in tool_calls:
        if not event.ok:
            continue
        args = payload.get("args") or {}
        if event.tool_name == CHECK_TOOL_NAME:
            try:
                verdict = _check_return(
                    str(args.get("order_status") or ""),
                    str(args.get("reason") or ""),
                    int(args.get("days_since_delivery")),
                )
            except (TypeError, ValueError):
                continue
            clause_id = str(verdict.get("clause_id") or "")
            if clause_id:
                _add(clause_id, _clause_version(db, clause_id, merchant_id))
        elif event.tool_name == SEARCH_TOOL_NAME:
            query = str(args.get("query") or "")
            top_k = int(args.get("top_k") or 5)
            for clause_id, version in _replay_policy_search(
                db, merchant_id, query, top_k
            ):
                _add(clause_id, version)
    return citations


# ---------------------------------------------------------------------------
# 感知层重放
# ---------------------------------------------------------------------------


def _collect_percepts(
    messages: list[ChatMessage],
) -> list[dict[str, Any]]:
    """用 user chat_message 原文重放感知层(纯函数),字段与图内 Percept 一致。"""
    percepts: list[dict[str, Any]] = []
    for message in messages:
        if message.role != "user":
            continue
        item: dict[str, Any] = {
            "message_id": message.id,
            "created_at": _iso(message.created_at),
        }
        try:
            percept = normalize(
                message.content,
                attachments=list(message.attachments or []),
            )
        except (TypeError, ValueError) as exc:
            # 历史非法输入(如空文本)不阻断审计:字段显式 null + 错误类型
            item.update(
                {
                    "text": None,
                    "attachments": None,
                    "locale": None,
                    "channel": None,
                    "truncated": None,
                    "original_length": None,
                    "flags": None,
                    "error": type(exc).__name__,
                }
            )
        else:
            item.update(percept.model_dump())
        percepts.append(item)
    return percepts


# ---------------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------------


def _collect_timeline(
    messages: list[ChatMessage],
    orchestration: list[tuple[ToolEvent, dict[str, Any]]],
    tool_calls: list[tuple[ToolEvent, dict[str, Any]]],
    confirm: ConfirmRequest | None,
    refund: RefundRequest | None,
    ticket: Ticket | None,
) -> list[dict[str, Any]]:
    """把消息 / 事件 / 确认卡 / 业务单合成一条按时间排序的时间线。"""
    items: list[dict[str, Any]] = []
    sequence = {"value": 0}

    def _add(
        at: datetime | None,
        stage: str,
        kind: str,
        *,
        name: str | None = None,
        actor: str | None = None,
        ref: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        sequence["value"] += 1
        items.append(
            {
                "at": _iso(at),
                "stage": stage,
                "kind": kind,
                "name": name,
                "actor": actor,
                "ref": ref,
                "detail": detail,
                "_sort_at": _as_utc(at),
                "_sort_seq": sequence["value"],
            }
        )

    for message in messages:
        if message.role == "user":
            _add(
                message.created_at,
                "percept",
                "message",
                name="chat_message",
                actor="customer",
                ref=message.id,
                detail={
                    "role": message.role,
                    "intent": message.intent,
                    "content": message.content,
                },
            )
        elif message.role == "assistant":
            _add(
                message.created_at,
                "compose",
                "message",
                name="chat_message",
                actor="assistant",
                ref=message.id,
                detail={
                    "role": message.role,
                    "intent": message.intent,
                    "content": message.content,
                },
            )
        else:
            _add(
                message.created_at,
                "system",
                "message",
                name="chat_message",
                actor="system",
                ref=message.id,
                detail={"role": message.role, "content": message.content},
            )

    for event, payload in orchestration:
        event_name = str(payload.get("event") or "")
        _add(
            event.ts,
            _EVENT_STAGE.get(event_name, "act"),
            "event",
            name=event_name,
            actor="system",
            ref=event.id,
            detail={
                "step": event.step,
                "ok": bool(event.ok),
                "error_code": event.error_code,
                "payload": payload,
            },
        )

    for event, payload in tool_calls:
        if event.tool_name in _RETRIEVE_TOOLS:
            stage = "retrieve"
        elif event.tool_name in _WRITE_TOOLS:
            stage = "confirm"
        else:
            stage = "tool"
        _add(
            event.ts,
            stage,
            "tool_call",
            name=event.tool_name,
            actor="tool",
            ref=event.id,
            detail={
                "step": event.step,
                "ok": bool(event.ok),
                "error_code": event.error_code,
                "args": payload.get("args"),
            },
        )

    if confirm is not None:
        _add(
            confirm.created_at,
            "confirm",
            "confirm_card",
            name=confirm.tool_name,
            actor="confirm_gate",
            ref=confirm.confirm_token,
            detail={
                "status": confirm.status,
                "expires_at": _iso(confirm.expires_at),
                "what": confirm.what,
            },
        )
    if refund is not None:
        _add(
            refund.created_at,
            "receipt",
            "business_record",
            name="refund_request",
            actor="business",
            ref=refund.id,
            detail={
                "order_id": refund.order_id,
                "amount_cents": refund.amount_cents,
                "status": refund.status,
            },
        )
    if ticket is not None:
        _add(
            ticket.created_at,
            "receipt",
            "business_record",
            name="ticket",
            actor="business",
            ref=ticket.id,
            detail={"order_id": ticket.order_id, "status": ticket.status},
        )

    # 时间缺失的条目排最前(不臆造时间);同一时刻按写入顺序稳定排序
    items.sort(
        key=lambda item: (
            item["_sort_at"] or datetime.min.replace(tzinfo=timezone.utc),
            item["_sort_seq"],
        )
    )
    for item in items:
        item.pop("_sort_at", None)
        item.pop("_sort_seq", None)
    return items


# ---------------------------------------------------------------------------
# 主构建函数
# ---------------------------------------------------------------------------


def build_audit_report(
    db: Session,
    *,
    trace_id: str | None = None,
    refund_id: str | None = None,
) -> dict[str, Any]:
    """构建单案审计 JSON(trace_id / refund_id 二选一)。

    找不到时抛 ``AuditNotFound``;其余字段缺失一律显式 null。
    """
    if bool(trace_id) == bool(refund_id):
        raise ValueError("trace_id 与 refund_id 必须二选一")

    refund: RefundRequest | None = None
    if refund_id:
        refund = db.get(RefundRequest, str(refund_id))
        if refund is None:
            raise AuditNotFound(f"退款单不存在:{refund_id}")
        resolved = _trace_of_refund(db, refund)
        if not resolved:
            raise AuditNotFound(f"退款单未关联到可导出的 trace:{refund_id}")
        trace_id = resolved

    assert trace_id is not None  # 上游已保证二选一
    events = trace_events(db, trace_id)
    messages = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.trace_id == trace_id)
            .order_by(ChatMessage.created_at)
        )
    )
    if not events and not messages:
        raise AuditNotFound(f"trace 无任何审计记录:{trace_id}")

    orchestration, tool_calls = _split_events(events)
    if refund is None:
        refund = _refund_of_trace(db, events)
    ticket = _ticket_of_trace(db, orchestration)
    execute_event, execute_payload = _execute_event(orchestration)
    confirm = _find_confirm(db, orchestration, tool_calls)

    session_id: str | None = None
    for message in messages:
        if message.session_id:
            session_id = str(message.session_id)
            break
    if session_id is None and ticket is not None and ticket.session_id:
        session_id = str(ticket.session_id)
    merchant_id: str | None = None
    if session_id:
        session_row = db.get(ChatSession, session_id)
        if session_row is not None and session_row.merchant_id:
            merchant_id = str(session_row.merchant_id)

    # ---- 执行者:模型 / 规则路径 / 人工坐席 ----
    model: str | None = None
    rules: list[str] = []
    for event, payload in orchestration:
        if payload.get("event") != EVENT_CLASSIFY:
            continue
        if payload.get("model"):
            model = str(payload["model"])
        path = str(payload.get("path") or "")
        if path and path != "llm" and path not in rules:
            rules.append(path)
    agent: str | None = None
    if refund is not None and refund.decided_by:
        agent = str(refund.decided_by)
    elif ticket is not None and ticket.claimed_by:
        agent = str(ticket.claimed_by)

    # ---- 确认卡与状态迁移 ----
    transitions: list[dict[str, Any]] = []
    if confirm is not None:
        transitions.append(
            {"from": None, "to": "pending", "at": _iso(confirm.created_at)}
        )
        if confirm.status == "confirmed":
            transitions.append(
                {
                    "from": "pending",
                    "to": "confirmed",
                    "at": _iso(execute_event.ts) if execute_event is not None else None,
                }
            )
        elif confirm.status in ("cancelled", "expired"):
            # 拒绝/超时没有落库时间戳(表无迁移历史),时间显式 null
            transitions.append({"from": "pending", "to": confirm.status, "at": None})
    confirm_section = {
        "token": None if confirm is None else confirm.confirm_token,
        "status": None if confirm is None else confirm.status,
        "transitions": transitions,
        "decided_by": agent,
    }

    # ---- 退款单与回执(回读值) ----
    refund_section: dict[str, Any] = {
        "refund_id": None,
        "amount_cents": None,
        "status": None,
        "receipt": None,
    }
    if refund is not None:
        executed_result_id = str(execute_payload.get("result_id") or "")
        refund_section = {
            "refund_id": refund.id,
            "amount_cents": refund.amount_cents,
            "status": refund.status,
            "receipt": {
                "kind": "refund",
                "refund_request_id": refund.id,
                "order_id": refund.order_id,
                "amount_cents": refund.amount_cents,
                "status": refund.status,
                "occurred_at_utc": _iso(refund.created_at),
                "trace_id": trace_id,
                # 回读核对:执行事件明确指向这张退款单才算 verified
                "verified": executed_result_id == refund.id,
                "idempotent_hit": (
                    execute_payload.get("idempotent_hit")
                    if execute_payload
                    else None
                ),
            },
        }

    # ---- 完整性:七段是否都有真实落库痕迹 ----
    present: set[str] = set()
    if any(message.role == "user" for message in messages):
        present.add("percept")
    if any(
        payload.get("event") in (EVENT_CLASSIFY, EVENT_GRAPH_PROMPT_BLOCK)
        for _, payload in orchestration
    ):
        present.add("classify")
    if any(
        event.tool_name in _RETRIEVE_TOOLS
        for event, _ in tool_calls
    ) or any(
        payload.get("event") == EVENT_QUERY_REWRITE for _, payload in orchestration
    ):
        present.add("retrieve")
    if tool_calls:
        present.add("tool")
    if confirm is not None:
        present.add("confirm")
    if execute_event is not None:
        present.add("execute")
    if refund is not None or ticket is not None:
        present.add("receipt")

    stages_present = [stage for stage in STAGE_ORDER if stage in present]
    missing_stages = [stage for stage in STAGE_ORDER if stage not in present]

    return {
        "case": {
            "trace_id": trace_id,
            "refund_id": None if refund is None else refund.id,
            "session_id": session_id,
            "ticket_id": None if ticket is None else ticket.id,
            "exported_at": _iso(utc_now()),
            "schema_version": 1,
        },
        "actors": {"model": model, "rules": rules, "agent": agent},
        "timeline": _collect_timeline(
            messages, orchestration, tool_calls, confirm, refund, ticket
        ),
        "percepts": _collect_percepts(messages),
        "events": [
            {
                "event": str(payload.get("event") or ""),
                "step": event.step,
                "ok": bool(event.ok),
                "error_code": event.error_code,
                "payload": payload,
                "at": _iso(event.ts),
            }
            for event, payload in orchestration
        ],
        "tool_events": [
            {
                "tool": event.tool_name,
                "payload": payload,
                "ok": bool(event.ok),
                "created_at": _iso(event.ts),
            }
            for event, payload in tool_calls
        ],
        "citations": _collect_citations(db, tool_calls, merchant_id),
        "confirm": confirm_section,
        "refund": refund_section,
        "integrity": {
            "stages_present": stages_present,
            "missing_stages": missing_stages,
            "complete": not missing_stages,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _db_url(db_arg: str | None) -> str:
    """--db 解析成 sqlite URL;缺省沿用项目 DB 解析方式。"""
    if not db_arg:
        return get_database_url()
    path = Path(db_arg).expanduser().resolve()
    if not path.is_file():
        raise AuditNotFound(f"数据库文件不存在:{path}")
    return f"sqlite:///{path.as_posix()}"


def _configure_streams() -> None:
    """Windows 管道默认 GBK:强制 stdout/stderr 走 UTF-8,保证 JSON 可被解析。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - 非文本流兜底
                pass


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:成功打印一个 JSON 对象并返回 0;失败只写 stderr 并返回 2。"""
    _configure_streams()
    parser = argparse.ArgumentParser(
        description="导出单案审计链 JSON(trace 或退款单二选一)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--trace-id", help="按 trace_id 导出整条链路")
    group.add_argument("--refund-id", help="按退款单号反查 trace 并导出")
    parser.add_argument("--db", help="SQLite 数据库路径(缺省 backend/dev.db)")
    parser.add_argument("--pretty", action="store_true", help="缩进 2 空格输出")
    args = parser.parse_args(argv)

    try:
        engine = create_db_engine(_db_url(args.db))
    except AuditNotFound as exc:
        print(f"导出失败:{exc}", file=sys.stderr)
        return 2
    with Session(engine) as db:
        try:
            report = build_audit_report(
                db, trace_id=args.trace_id, refund_id=args.refund_id
            )
        except AuditNotFound as exc:
            print(f"导出失败:{exc}", file=sys.stderr)
            return 2

    text = json.dumps(
        report, ensure_ascii=False, indent=2 if args.pretty else None
    )
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
