"""w1_router.py — W2-C 的"MOCK 直调"编排基线(六节点的临时替身)。

⚠️ MOCK 声明
本文件是**无图直调**基线:意图理解走 ``FakeLLM`` 规则表,动作按 if/else 顺序直调
W2 工具,**没有 LangGraph、没有真实模型**。W3-A 将把它整体替换为六节点图
(perceive → classify → retrieve → act → compose → handoff);这里刻意保持"一个流程
一段代码"的直白写法,方便 W3 逐节点对照迁移。

本文件负责的四件事(对应任务书验收的三场景):
1. 政策问答:``policy__search``(无命中时确定性改写再查,MOCK 替代 LLM query rewrite)
   → 组装带 ``[n]`` 引用与 ``citations`` 映射的回复;
2. 订单判责:``shop__get_order`` → ``policy__check_return`` → 符合则 ``refund__create``
   生成**确认卡**(真正落库要等 W3 的确认流执行 ``execute``);不符合只解释+引用条款;
3. 转人工:三触发(明示人工 / 连续 3 轮未解决 / 金额争议)→ ``handoff__create_ticket``
   确认卡 + 会话 ``status='handed_off'``,此后入口直接返回固定话术、AI 不再回复;
4. 每步(意图判定 / 查询改写 / 工具调用 / 状态变更)都写统一 trace_id 的结构化事件。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utc_now
from app.llm import (
    INTENT_HANDOFF,
    INTENT_ORDER_JUDGE,
    INTENT_OTHER,
    INTENT_POLICY_QA,
    SCHEMA_INTENT,
    SCHEMA_REPLY,
    FakeLLM,
    LLMClient,
    build_facts_message,
)
from app.llm.base import RISK_HINTS
from app.models import ChatMessage, ChatSession, KnowledgeItem
from app.tools import ToolContext, invoke
from app.trace import (
    EVENT_CLASSIFY,
    EVENT_QUERY_REWRITE,
    EVENT_ROUTE_BLOCKED,
    EVENT_SESSION_STATUS,
    record_event,
)

# ---------------------------------------------------------------------------
# 对外常量:路由层(api/chat.py)与测试共用
# ---------------------------------------------------------------------------

#: 会话已 handed_off 后的固定话术:AI 不再自动回复,避免"人工已接手还在自动答"。
HANDOFF_BLOCKED_REPLY = (
    "您的问题已转交人工客服处理,工单已进入人工队列,坐席会尽快回复。"
    "为避免与人工重复,我先不再自动作答;如需补充信息请直接留言。"
)

#: 高置信快捷路径:命中即返回 conf=1.0,且**必须**被路由消费(跳过 FakeLLM)。
#: 只放"无需理解就能确定"的显式表达,杜绝"硬编码低置信度却无人消费"的反模式。
HIGH_CONFIDENCE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        INTENT_HANDOFF,
        ("转人工", "人工客服", "真人客服", "找人工", "人工坐席", "转接人工"),
    ),
)

#: 可退原因抽取规则(顺序即优先级:先具体破损场景,再通用质量问题,最后无理由)。
REASON_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("物流破损", ("物流破损", "外包装破损", "运输破损", "压坏了")),
    ("错发漏发", ("错发", "漏发", "发错", "少发", "少件")),
    ("质量问题", ("质量问题", "线头", "开胶", "脱线", "起球", "坏", "破洞")),
    ("七天无理由", ("无理由", "不喜欢", "买大", "买小", "不想要", "尺码不符", "不想要了")),
)

#: 无命中时的确定性查询改写(MOCK:替代 W4 的 LLM query rewrite;改写词来自政策文档)。
QUERY_REWRITES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("运费", "邮费"), "退货运费 商家 承担"),
    (("尺码", "多高", "多重", "码数", "偏大", "偏小", "买大", "买小"), "尺码不符 换货"),
    (("无理由", "几天", "多久", "多少天", "期限"), "无理由退货 期限内 几天"),
    (("质量", "线头", "开胶", "脱线"), "质量问题 退货 期限"),
)

_ORDER_ID_RE = re.compile(r"\b(?:seed-order-|ORD-)[A-Za-z0-9-]{2,63}\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"(?:实付|付了|按|退我|退给)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?")


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    """一轮对话的编排结果(路由层原样返回给调用方,不做二次加工)。"""

    intent: str
    confidence: float
    intent_reason: str
    classification_path: str  # fast_path / llm / rule:unresolved_3 / route_guard
    reply: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    pending_confirm: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    session_status: str = "active"
    ai_replied: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "intent_reason": self.intent_reason,
            "classification_path": self.classification_path,
            "reply": self.reply,
            "citations": self.citations,
            "pending_confirm": self.pending_confirm,
            "tool_calls": self.tool_calls,
            "session_status": self.session_status,
            "ai_replied": self.ai_replied,
        }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def handle_turn(
    *,
    session: Session,
    session_row: ChatSession,
    percept_text: str,
    context: dict[str, Any] | None = None,
    trace_id: str,
    llm: LLMClient | None = None,
) -> TurnResult:
    """处理一轮进线。调用前路由层已落库 user 消息并做过会话状态守卫。"""
    context = context or {}
    llm = llm or FakeLLM()
    ctx = ToolContext(
        session=session,
        merchant_id=session_row.merchant_id,
        customer_id=session_row.customer_id,
        trace_id=trace_id,
    )
    calls: list[dict[str, Any]] = []

    order_id = _resolve_order_id(percept_text, context)
    facts = {
        "text": percept_text,
        "has_order": bool(order_id),
        "unresolved_rounds": int(context.get("unresolved_rounds") or 0),
    }

    # ---- 1. 意图判定:高置信快捷路径优先,其余交给 LLM(MOCK: 规则表) ----
    decision, path = _classify(llm, percept_text, facts)
    # 连续 3 轮同意图未解决 → 升级转人工(消费历史消息,不靠关键词"三遍")
    if decision["intent"] != INTENT_HANDOFF:
        prior_same = _count_prior_same_intent(session, session_row.id, decision["intent"])
        if prior_same + 1 >= 3:
            decision = {
                "intent": INTENT_HANDOFF,
                "confidence": 0.95,
                "reason": "连续 3 轮同意图未解决",
            }
            path = "rule:unresolved_3"
    record_event(
        session,
        trace_id=trace_id,
        event=EVENT_CLASSIFY,
        payload={
            "path": path,
            "intent": decision["intent"],
            "confidence": decision["confidence"],
            "reason": decision["reason"],
            "model": llm.model_name,
        },
    )

    intent = decision["intent"]
    if intent == INTENT_HANDOFF:
        return _handle_handoff(
            session=session,
            ctx=ctx,
            llm=llm,
            text=percept_text,
            context=context,
            order_id=order_id,
            reason=decision["reason"],
            trace_id=trace_id,
            session_row=session_row,
            decision=decision,
            path=path,
            calls=calls,
        )
    if intent == INTENT_ORDER_JUDGE:
        return _handle_order(
            session=session,
            ctx=ctx,
            llm=llm,
            text=percept_text,
            context=context,
            order_id=order_id,
            trace_id=trace_id,
            session_row=session_row,
            decision=decision,
            path=path,
            calls=calls,
        )
    if intent == INTENT_POLICY_QA:
        return _handle_policy(
            session=session,
            ctx=ctx,
            llm=llm,
            text=percept_text,
            trace_id=trace_id,
            session_row=session_row,
            decision=decision,
            path=path,
            calls=calls,
        )
    return _handle_other(
        llm=llm, session_row=session_row, decision=decision, path=path, calls=calls
    )


def build_blocked_turn(
    *,
    session: Session,
    session_row: ChatSession,
    trace_id: str,
    text: str,
) -> TurnResult:
    """会话非 active 时的入口守卫:写拦截事件,返回固定话术,AI 不回复。"""
    record_event(
        session,
        trace_id=trace_id,
        event=EVENT_ROUTE_BLOCKED,
        payload={"session_status": session_row.status, "text_length": len(text)},
    )
    return TurnResult(
        intent="blocked",
        confidence=1.0,
        intent_reason="会话非 active(已转人工),AI 停止自动回复",
        classification_path="route_guard",
        reply=HANDOFF_BLOCKED_REPLY,
        session_status=session_row.status,
        ai_replied=False,
    )


# ---------------------------------------------------------------------------
# 各场景处理
# ---------------------------------------------------------------------------


def _handle_policy(
    *,
    session: Session,
    ctx: ToolContext,
    llm: LLMClient,
    text: str,
    trace_id: str,
    session_row: ChatSession,
    decision: dict[str, Any],
    path: str,
    calls: list[dict[str, Any]],
) -> TurnResult:
    """政策问答:检索 → (无命中)确定性改写再检索 → 带 [n] 引用组装回复。"""
    result = invoke("policy__search", {"query": text}, ctx)
    calls.append(_call_record("policy__search", result))

    if result.ok and result.data.get("no_hit"):
        rewritten = _rewrite_query(text)
        if rewritten:
            record_event(
                session,
                trace_id=trace_id,
                event=EVENT_QUERY_REWRITE,
                payload={"original": text, "rewritten": rewritten, "strategy": "keyword_map(MOCK)"},
            )
            retry = invoke("policy__search", {"query": rewritten}, ctx)
            calls.append(_call_record("policy__search", retry, note=f"改写检索:{rewritten}"))
            if retry.ok and not retry.data.get("no_hit"):
                result = retry

    if not result.ok:
        return _turn(
            decision,
            path,
            "政策检索服务暂时不可用,请稍后重试或转人工客服。",
            [],
            None,
            calls,
            session_row,
        )

    no_hit = bool(result.data.get("no_hit"))
    clauses = [] if no_hit else list(result.data.get("results") or [])[:3]
    citations = [
        {
            "index": i,
            "clause_id": c.get("clause_id"),
            "version": c.get("version"),
            "snippet": str(c.get("text") or "")[:120],
        }
        for i, c in enumerate(clauses, start=1)
    ]
    reply = _render(
        llm,
        {"kind": "policy_answer", "no_hit": no_hit, "clauses": clauses},
    )
    return _turn(decision, path, reply, citations, None, calls, session_row)


def _handle_order(
    *,
    session: Session,
    ctx: ToolContext,
    llm: LLMClient,
    text: str,
    context: dict[str, Any],
    order_id: str | None,
    trace_id: str,
    session_row: ChatSession,
    decision: dict[str, Any],
    path: str,
    calls: list[dict[str, Any]],
) -> TurnResult:
    """订单判责:查单 → (金额争议则转人工) 查政策 → 符合出确认卡 / 不符合解释。"""
    if not order_id:
        reply = _render(llm, {"kind": "order_missing_id"})
        return _turn(decision, path, reply, [], None, calls, session_row)

    got = invoke("shop__get_order", {"order_id": order_id}, ctx)
    calls.append(_call_record("shop__get_order", got))
    if not got.ok:
        reply = _render(llm, {"kind": "order_not_found"})
        return _turn(decision, path, reply, [], None, calls, session_row)

    order = got.data
    total_cents = int(order.get("total_cents") or 0)

    # 金额争议触发转人工:有风险信号 + 客户声称金额与实付不符(不按客户口径退)。
    claimed = _claimed_amounts_cents(text, context)
    if _has_risk(text) and claimed and any(c != total_cents for c in claimed):
        return _handle_handoff(
            session=session,
            ctx=ctx,
            llm=llm,
            text=text,
            context=context,
            order_id=order_id,
            reason="金额争议:客户声称金额与订单实付不符",
            trace_id=trace_id,
            session_row=session_row,
            decision=decision,
            path=path,
            calls=calls,
            order=order,
        )

    reason_text = _extract_reason(text)
    days = _days_since_delivery(context, order.get("created_at"))
    checked = invoke(
        "policy__check_return",
        {
            "order_status": order.get("status"),
            "reason": reason_text,
            "days_since_delivery": days,
        },
        ctx,
    )
    calls.append(_call_record("policy__check_return", checked))

    clause_id = (checked.data or {}).get("clause_id") if checked.ok else None
    clause = _clause(session, ctx.merchant_id, clause_id)
    citations = (
        [{"index": 1, "clause_id": clause["clause_id"], "version": clause["version"],
          "snippet": clause["text"][:120]}]
        if clause
        else []
    )

    if checked.ok and checked.data.get("eligible"):
        amount = int(context.get("refund_amount_cents") or total_cents)
        created = invoke(
            "refund__create",
            {"order_id": order_id, "amount_cents": amount, "reason": reason_text},
            ctx,
        )
        calls.append(_call_record("refund__create", created))
        pending = created.data.get("pending_confirm") if created.ok else None
        if created.ok:
            reply = _render(
                llm,
                {
                    "kind": "order_verdict_eligible",
                    "order_id": order_id,
                    "status": order.get("status"),
                    "amount_cents": amount,
                    "reason_text": reason_text,
                    "verdict_reason": checked.data.get("reason"),
                    "clause": clause,
                },
            )
        else:
            reply = "退款申请未能创建(工具返回失败),请稍后重试或转人工客服。"
        return _turn(decision, path, reply, citations, pending, calls, session_row)

    verdict_reason = (checked.data or {}).get("reason") or "未能完成退换货判定"
    reply = _render(
        llm,
        {
            "kind": "order_verdict_refused",
            "order_id": order_id,
            "status": order.get("status"),
            "verdict_reason": verdict_reason,
            "clause": clause,
        },
    )
    return _turn(decision, path, reply, citations, None, calls, session_row)


def _handle_handoff(
    *,
    session: Session,
    ctx: ToolContext,
    llm: LLMClient,
    text: str,
    context: dict[str, Any],
    order_id: str | None,
    reason: str,
    trace_id: str,
    session_row: ChatSession,
    decision: dict[str, Any],
    path: str,
    calls: list[dict[str, Any]],
    order: dict[str, Any] | None = None,
) -> TurnResult:
    """转人工:建工单(确认卡) + 会话 handed_off → 此后 AI 不再回复。"""
    if order is None and order_id:
        got = invoke("shop__get_order", {"order_id": order_id}, ctx)
        calls.append(_call_record("shop__get_order", got))
        if got.ok:
            order = got.data
        else:
            order_id = None  # 查不到/越权时不要把订单号塞进工单,避免二次失败

    label = _friendly_reason(reason)
    summary = _handoff_summary(reason=label, order=order, text=text, context=context)
    ticket = invoke(
        "handoff__create_ticket",
        {"reason": label, "summary": summary, "order_id": order_id},
        ctx,
    )
    calls.append(_call_record("handoff__create_ticket", ticket))
    pending = ticket.data.get("pending_confirm") if ticket.ok else None

    if ticket.ok:
        previous = session_row.status
        session_row.status = "handed_off"
        record_event(
            session,
            trace_id=trace_id,
            event=EVENT_SESSION_STATUS,
            payload={"from": previous, "to": "handed_off", "reason": label},
        )
    reply = _render(llm, {"kind": "handoff", "reason": label, "order_id": order_id})
    result = _turn(decision, path, reply, [], pending, calls, session_row)
    result.session_status = "handed_off"
    return result


def _handle_other(
    *,
    llm: LLMClient,
    session_row: ChatSession,
    decision: dict[str, Any],
    path: str,
    calls: list[dict[str, Any]],
) -> TurnResult:
    reply = _render(llm, {"kind": "other"})
    return _turn(decision, path, reply, [], None, calls, session_row)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


#: 转接原因的客户可读文案(判定原因保留原始语义用于 trace)。
_REASON_LABELS = {
    "明示人工": "用户主动要求人工",
    "连续 3 轮同意图未解决": "连续 3 轮未解决",
    "金额争议:客户声称金额与订单实付不符": "金额争议:客户声称的退款金额与订单实付不符",
}


def _friendly_reason(reason: str) -> str:
    """把内部判定原因翻成客户可读的转接原因。"""
    return _REASON_LABELS.get(reason, reason)


def _classify(
    llm: LLMClient, text: str, facts: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """高置信快捷路径 → 命中返回 conf=1.0 并直接消费;否则调 LLM。"""
    shortcut = _shortcut_intent(text)
    if shortcut is not None:
        return (
            {"intent": shortcut, "confidence": 1.0, "reason": "明示人工"},
            "fast_path",
        )
    messages = [
        {"role": "system", "content": "你是售后客服的意图分类器,只输出 JSON。"},
        {"role": "user", "content": text},
        build_facts_message(facts),
    ]
    raw = llm.complete(messages, SCHEMA_INTENT)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - 规则表不会走到
        data = {"intent": INTENT_OTHER, "confidence": 0.0, "reason": "解析失败"}
    if data.get("intent") not in {INTENT_POLICY_QA, INTENT_ORDER_JUDGE, INTENT_HANDOFF, INTENT_OTHER}:
        data["intent"] = INTENT_OTHER
    return data, "llm"


def _shortcut_intent(text: str) -> str | None:
    for intent, hints in HIGH_CONFIDENCE_RULES:
        if any(h in text for h in hints):
            return intent
    return None


def _count_prior_same_intent(session: Session, session_id: str, intent: str) -> int:
    """本会话历史中已判为同一意图的 user 轮数(用于"连续 3 轮未解决"升级)。"""
    rows = session.scalars(
        select(ChatMessage.intent).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "user",
            ChatMessage.intent == intent,
        )
    ).all()
    return len(rows)


def _resolve_order_id(text: str, context: dict[str, Any]) -> str | None:
    """订单号来源:请求上下文(已解析的会话槽位)优先,其次消息文本中识别。"""
    explicit = context.get("order_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    match = _ORDER_ID_RE.search(text)
    return match.group(0) if match else None


def _extract_reason(text: str) -> str:
    for reason, hints in REASON_RULES:
        if any(h in text for h in hints):
            return reason
    return "七天无理由"


def _has_risk(text: str) -> bool:
    return any(h in text for h in RISK_HINTS)


def _claimed_amounts_cents(text: str, context: dict[str, Any]) -> list[int]:
    """客户声称的金额(分):上下文显式字段 + 文本"实付/按…元"表达。"""
    amounts: list[int] = []
    for key in ("claimed_refund_cents", "claimed_paid_cents"):
        try:
            value = int(context.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            amounts.append(value)
    for raw in _AMOUNT_RE.findall(text):
        try:
            amounts.append(int(round(float(raw) * 100)))
        except ValueError:  # pragma: no cover
            continue
    return amounts


def _days_since_delivery(context: dict[str, Any], created_at_iso: Any) -> int:
    """签收天数:上下文显式值优先(评测用例给定时),否则按订单创建时间推算。"""
    explicit = context.get("days_since_delivery")
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    dt = _parse_dt(created_at_iso)
    if dt is None:
        return 0
    days = (utc_now() - dt).days
    return max(0, int(days))


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rewrite_query(text: str) -> str | None:
    for hints, rewritten in QUERY_REWRITES:
        if any(h in text for h in hints):
            return rewritten
    return None


def _clause(session: Session, merchant_id: str, clause_id: Any) -> dict[str, Any] | None:
    """按 clause_id 取知识库原文(引用必须是真实存在的条款,查不到就不引用)。"""
    if not clause_id:
        return None
    row = session.scalars(
        select(KnowledgeItem).where(
            KnowledgeItem.merchant_id == merchant_id,
            KnowledgeItem.clause_id == str(clause_id),
            KnowledgeItem.deleted_at.is_(None),
        )
    ).first()
    if row is None:
        return None
    return {
        "clause_id": row.clause_id,
        "version": row.version,
        "title": row.title or "",
        "text": row.content,
    }


def _handoff_summary(
    *, reason: str, order: dict[str, Any] | None, text: str, context: dict[str, Any]
) -> str:
    parts = [f"转接原因:{reason}"]
    if order:
        parts.append(
            f"订单 {order.get('order_id')} 状态 {order.get('status')},"
            f"实付 {order.get('total_cents')} 分"
        )
    rounds = context.get("unresolved_rounds")
    if rounds:
        parts.append(f"已连续 {rounds} 轮未解决")
    parts.append(f"客户原话:{text[:120]}")
    return ";".join(parts)


def _render(llm: LLMClient, facts: dict[str, Any]) -> str:
    """让 LLM 产出回复文本(MOCK: FakeLLM 模板);失败时返回安全兜底文案。"""
    messages = [
        {"role": "system", "content": "你是售后客服,用中文回复,不要编造政策。"},
        build_facts_message(facts),
    ]
    try:
        raw = llm.complete(messages, SCHEMA_REPLY)
        reply = json.loads(raw).get("reply", "")
    except (json.JSONDecodeError, AttributeError):  # pragma: no cover
        reply = ""
    return reply or "抱歉,我暂时无法处理该问题,可以为您转接人工客服。"


def _call_record(name: str, result: Any, note: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "ok": bool(result.ok),
        "error_code": getattr(result.error, "code", None) if not result.ok else None,
    }
    if note:
        record["note"] = note
    return record


def _turn(
    decision: dict[str, Any],
    path: str,
    reply: str,
    citations: list[dict[str, Any]],
    pending_confirm: dict[str, Any] | None,
    calls: list[dict[str, Any]],
    session_row: ChatSession,
) -> TurnResult:
    return TurnResult(
        intent=decision["intent"],
        confidence=float(decision.get("confidence") or 0.0),
        intent_reason=str(decision.get("reason") or ""),
        classification_path=path,
        reply=reply,
        citations=citations,
        pending_confirm=pending_confirm,
        tool_calls=calls,
        session_status=session_row.status,
    )
