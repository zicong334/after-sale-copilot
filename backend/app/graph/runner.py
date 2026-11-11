"""runner.py — W3-A 图的对外入口(替换 W2-C 的 MOCK 直调编排)。

对外接口:

- ``run(thread_id, percept, ...)``:从一条已归一化的 ``Percept`` 起跑图;若图停在
  确认门(``interrupt``),返回的 ``TurnResult`` 里带 ``pending_confirm``(确认卡四要素
  + ``confirm_token``),同时 ``reply`` 是"待确认话术"——挂起那一轮的 HTTP 响应
  必须能直接回给用户;
- ``resume(thread_id, confirm_decision, ...)``:把人工决定送回确认门。``confirm_decision``
  可为 bool / ``"approve"`` / ``"confirm"`` / ``{"approved": true}``(无法识别按拒绝处理);
- ``handle_turn(...)`` / ``build_blocked_turn(...)``:与 W2-C 同签名的兼容层,
  只给 ``api/chat.py`` 用,业务全部走图。

恢复语义(P0-C2,必读)
----------------------

LangGraph 在 ``interrupt`` 处挂起时会把整个图状态存进 checkpointer;``resume`` 时
**被中断的节点会从头重跑一遍**(源码 ``langgraph/types.py:928`` 原文
"The graph resumes from the start of the node, re-executing all logic")。
所以本图把"建确认卡(有副作用:落 ConfirmRequest)"放在**上一圈**完成;确认门节点
进入时先 ``interrupt``、恢复后才 ``execute`` 真正的写工具。interrupt 之前的重跑
只读 ``state["confirm"]``,不产生任何副作用,因此:

* 同一 thread_id 在新进程里(新的 ``SqliteSaver`` 连接 + 新编译的图)可以继续恢复;
* 已执行的副作用不会重放:``ConfirmRequest.status`` 一旦 ``confirmed``,
  ``_execute_confirmed`` 直接返回 ``CONFIRM_NOT_PENDING``,不会二次执行;
* 对已完成的 thread 再次 ``resume`` 是幂等的(返回终态,不新增业务数据)。

``thread_id`` 一律取 ``chat_session.id``:一个会话一条图线程,跨轮复用 checkpointer
里的历史;但每轮初始输入写全所有 State 字段(避免上一轮 steps/observations 残留)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.errors import StateConflictError
from app.graph.build import RECURSION_LIMIT, build_graph, open_checkpointer
from app.graph.state import initial_state
from app.llm import (
    INTENT_HANDOFF,
    INTENT_ORDER_JUDGE,
    INTENT_OTHER,
    INTENT_POLICY_QA,
    LLMClient,
)
from app.models import ChatMessage, ChatSession, Ticket
from app.perception import Percept, normalize
from app.services import tickets as ticket_service
from app.trace import (
    EVENT_ROUTE_BLOCKED,
    EVENT_SESSION_STATUS,
    new_trace_id,
    record_event,
)

#: 四类意图(仅用于兜底校验;分类结果由 classify 节点给出)。
_INTENTS = (INTENT_POLICY_QA, INTENT_ORDER_JUDGE, INTENT_HANDOFF, INTENT_OTHER)


@dataclass
class TurnResult:
    """一轮对话的处理结果(HTTP 响应的业务部分;字段与 W2-C 对齐)。"""

    intent: str
    confidence: float
    intent_reason: str
    classification_path: str
    reply: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    pending_confirm: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    session_status: str = "active"
    ai_replied: bool = True
    #: W5 真静默:AI 不吐字时的"系统回执"文本(不是 AI 气泡),仅前端提示用。
    ack: str = ""
    #: W5:回执对应的工单 id(买家消息已追加进该工单);没关联到工单时为 None。
    ack_ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "intent_reason": self.intent_reason,
            "classification_path": self.classification_path,
            "reply": self.reply,
            "citations": list(self.citations),
            "pending_confirm": self.pending_confirm,
            "tool_calls": list(self.tool_calls),
            "session_status": self.session_status,
            "ai_replied": self.ai_replied,
            "ack": self.ack,
            "ack_ticket_id": self.ack_ticket_id,
        }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _prior_intent_counts(session: Session, session_id: str) -> dict[str, int]:
    """本会话历史里"用户消息"各意图的轮数(classify 的连续 3 轮升级规则要用)。"""
    rows = session.execute(
        select(ChatMessage.intent, func.count())
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "user",
            ChatMessage.intent.is_not(None),
        )
        .group_by(ChatMessage.intent)
    ).all()
    return {str(intent): int(count) for intent, count in rows if intent}


def _as_percept(percept: Percept | dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(percept, Percept):
        return percept.model_dump()
    if isinstance(percept, dict):
        return dict(percept)
    return normalize(str(percept)).model_dump()


def _execute(
    *,
    session: Session,
    session_row: ChatSession,
    thread_id: str,
    graph_input: Any,
    checkpointer: SqliteSaver | None,
    checkpoint_path: str | Path | None,
    llm: LLMClient | None,
) -> tuple[dict[str, Any], Any]:
    """编译图并跑一次 invoke(必要时自建 SqliteSaver,并负责关连接)。"""
    owned: SqliteSaver | None = None
    saver = checkpointer
    if saver is None:
        saver = owned = open_checkpointer(checkpoint_path)
    try:
        graph = build_graph(
            session=session,
            session_row=session_row,
            checkpointer=saver,
            llm=llm,
            prior_intent_counts=_prior_intent_counts(session, session_row.id),
        )
        cfg = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": RECURSION_LIMIT,
        }
        out = graph.invoke(graph_input, cfg)
        state = dict(out)
        snapshot = graph.get_state(cfg)
        # 挂起判定必须"下一个节点 + 中断"双条件(只有 next 说明还在队列里)
        if bool(snapshot.next) and bool(snapshot.interrupts):
            state["__suspended__"] = snapshot.interrupts[0].value
            state["__next__"] = tuple(snapshot.next)
        return state, snapshot
    finally:
        if owned is not None:
            owned.conn.close()


def _refresh_status(session: Session, session_row: ChatSession) -> str:
    try:
        session.refresh(session_row)
    except Exception:  # noqa: BLE001 - 刷新失败就用手上的值(session 可能已 close)
        pass
    return str(session_row.status or "active")


def _citations(state: dict[str, Any]) -> list[dict[str, Any]]:
    """政策引用:索引从 1 开始,clause_id 必须来自真实检索结果。"""
    citations: list[dict[str, Any]] = []
    for index, clause in enumerate(state.get("retrieved") or [], start=1):
        citations.append(
            {
                "index": index,
                "clause_id": clause.get("clause_id"),
                "version": clause.get("version"),
                "snippet": str(clause.get("text") or "")[:120],
            }
        )
    return citations


def _pending_confirm(state: dict[str, Any]) -> dict[str, Any] | None:
    """挂起时把确认卡投影成对外结构(四要素 + token),与 W2-C 的字段一致。"""
    confirm = state.get("confirm") or {}
    if not confirm or confirm.get("decided"):
        return None
    card = dict(confirm.get("card") or {})
    if not card:
        return None
    pending = dict(card)
    pending["confirm_token"] = str(confirm.get("token") or "")
    return pending


def _to_result(state: dict[str, Any], session_status: str) -> TurnResult:
    intent = dict(state.get("intent") or {})
    name = str(intent.get("name") or INTENT_OTHER)
    return TurnResult(
        intent=name if name in _INTENTS else INTENT_OTHER,
        confidence=float(intent.get("confidence") or 0.0),
        intent_reason=str(intent.get("reason") or ""),
        classification_path=str(intent.get("path") or ""),
        reply=str(state.get("draft") or ""),
        citations=_citations(state),
        pending_confirm=_pending_confirm(state),
        tool_calls=list(state.get("observations") or []),
        session_status=session_status,
        ai_replied=True,
    )


def _get_session_row(
    session: Session, *, thread_id: str, session_row: ChatSession | None
) -> ChatSession:
    if session_row is not None:
        return session_row
    row = session.get(ChatSession, thread_id)
    if row is None:
        raise ValueError(f"chat_session 不存在:thread_id={thread_id!r}")
    return row


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def run(
    thread_id: str,
    percept: Percept | dict[str, Any] | str,
    *,
    session: Session | None = None,
    session_row: ChatSession | None = None,
    context: dict[str, Any] | None = None,
    trace_id: str | None = None,
    llm: LLMClient | None = None,
    checkpointer: SqliteSaver | None = None,
    checkpoint_path: str | Path | None = None,
    auto_commit: bool = True,
) -> TurnResult:
    """从一条进线消息起跑图(thread_id = chat_session.id)。"""
    own_session = session is None
    session = session or SessionLocal()
    try:
        row = _get_session_row(session, thread_id=thread_id, session_row=session_row)
        state, _ = _execute(
            session=session,
            session_row=row,
            thread_id=thread_id,
            graph_input=initial_state(
                text=str(_as_percept(percept).get("text") or ""),
                context=context,
                trace_id=trace_id or new_trace_id(),
            ),
            checkpointer=checkpointer,
            checkpoint_path=checkpoint_path,
            llm=llm,
        )
        result = _to_result(state, _refresh_status(session, row))
        if auto_commit:
            session.commit()
        return result
    finally:
        if own_session:
            session.close()


def resume(
    thread_id: str,
    confirm_decision: Any,
    *,
    session: Session | None = None,
    session_row: ChatSession | None = None,
    llm: LLMClient | None = None,
    checkpointer: SqliteSaver | None = None,
    checkpoint_path: str | Path | None = None,
    auto_commit: bool = True,
) -> TurnResult:
    """把人工确认决定送回确认门;返回终态结果(幂等:重复 resume 不重放副作用)。"""
    own_session = session is None
    session = session or SessionLocal()
    try:
        row = _get_session_row(session, thread_id=thread_id, session_row=session_row)
        state, _ = _execute(
            session=session,
            session_row=row,
            thread_id=thread_id,
            graph_input=Command(resume=confirm_decision),
            checkpointer=checkpointer,
            checkpoint_path=checkpoint_path,
            llm=llm,
        )
        result = _to_result(state, _refresh_status(session, row))
        if auto_commit:
            session.commit()
        return result
    finally:
        if own_session:
            session.close()


def handle_turn(
    *,
    session: Session,
    session_row: ChatSession,
    percept_text: str,
    context: dict[str, Any] | None = None,
    trace_id: str,
    llm: LLMClient | None = None,
    checkpointer: SqliteSaver | None = None,
    checkpoint_path: str | Path | None = None,
    auto_commit: bool = False,
) -> TurnResult:
    """兼容层(api/chat.py 调用):一轮消息 → 一次图执行。"""
    return run(
        session_row.id,
        percept_text,
        session=session,
        session_row=session_row,
        context=context,
        trace_id=trace_id,
        llm=llm,
        checkpointer=checkpointer,
        checkpoint_path=checkpoint_path,
        auto_commit=auto_commit,
    )


def _latest_open_ticket(session: Session, session_row: ChatSession) -> Ticket | None:
    """定位该会话对应的最近一个未关闭工单。

    W13-ISS-20 起优先按 ``ticket.session_id`` 精确匹配(W5-FIX R1 起 handoff 建单
    会写当前 thread_id),避免同一买家多会话时误把别的会话的工单释放/追加;
    匹配不到再回退到历史口径(merchant_id + customer_id,且 session_id 为空的
    0005 前老工单)。"最近"= last_msg_at 降序(NULL 排最后),同值再按 created_at
    降序。
    """

    def _query(*criteria: Any) -> Ticket | None:
        return session.scalars(
            select(Ticket)
            .where(*criteria, Ticket.status != "closed")
            .order_by(
                # 非 NULL 优先(兼容不支持 "NULLS LAST" 的方言)
                Ticket.last_msg_at.is_(None),
                Ticket.last_msg_at.desc(),
                Ticket.created_at.desc(),
            )
            .limit(1)
        ).first()

    ticket = _query(Ticket.session_id == session_row.id)
    if ticket is not None:
        return ticket
    return _query(
        Ticket.merchant_id == session_row.merchant_id,
        Ticket.customer_id == session_row.customer_id,
        Ticket.session_id.is_(None),
    )


def build_blocked_turn(
    *,
    session: Session,
    session_row: ChatSession,
    trace_id: str,
    text: str,
) -> TurnResult:
    """会话已 handed_off 时的路由守卫:AI 真静默,买家文本只追加到工单。

    W5 修正一:不再回固定话术(``reply=""``、``ai_replied=False``),避免与人工重复;
    买家这条消息照常落 ``chat_message``,并追加到该会话最近一个未关闭工单
    (``ticket_message.author_type='user'``);返回轻量 ``ack`` 供前端提示。
    全轮只写 1 条 ``EVENT_ROUTE_BLOCKED`` trace,不跑图、不调工具。
    """
    ack = "消息已记录,坐席会尽快跟进。"
    ack_ticket_id: str | None = None
    ticket = _latest_open_ticket(session, session_row)
    if ticket is not None:
        # 守卫式写入(关单后会被 tickets.add_message 拒绝),不在这里重写状态机
        ticket_service.add_message(session, ticket.id, "user", text)
        ack_ticket_id = ticket.id
        ack = f"消息已追加到工单 #{ticket.id},坐席会尽快跟进。"

    record_event(
        session,
        trace_id=trace_id,
        event=EVENT_ROUTE_BLOCKED,
        payload={"session_status": session_row.status, "text_length": len(text or "")},
    )
    session.flush()
    return TurnResult(
        intent="blocked",
        confidence=1.0,
        intent_reason="会话已转人工,不再自动作答",
        classification_path="route_guard",
        reply="",
        citations=[],
        pending_confirm=None,
        tool_calls=[],
        session_status=str(session_row.status or "active"),
        ai_replied=False,
        ack=ack,
        ack_ticket_id=ack_ticket_id,
    )


#: 买家主动转回 AI 时的固定话术(W13-ISS-20)。
RESUME_AI_REPLY = "好的,已结束人工服务,由我继续为您服务。您的问题可以直接告诉我。"


def build_resume_turn(
    *,
    session: Session,
    session_row: ChatSession,
    trace_id: str,
    text: str,
) -> TurnResult:
    """会话 handed_off 时买家主动要求"转回 AI"的逆向路由(W13-ISS-20)。

    验收要求:买家输入"退出人工/转回 AI/结束人工"等关键词时,必须
    「工单释放/取消 + 会话回 active + AI 接管」,而不是继续静默留言。

    实现口径:
    * 工单不改状态机枚举(``ticket.status`` 的 CHECK 只有 queued/claimed/
      waiting_customer/closed,加 cancelled 需重建表,风险大于收益),用既有
      ``close()`` 完成"释放"语义;释放前先追加一条 internal 的 system 消息,
      保留"为什么关单"的审计痕迹。
    * 会话状态回写 ``active`` 并记一条 ``session__status_change`` 事件,
      事件载荷只放状态与原因,不放买家原文(隐私红线)。
    * 不跑图、不调工具:本轮回一句固定话术,下一轮起恢复正常路由。
    """
    ticket = _latest_open_ticket(session, session_row)
    ack = RESUME_AI_REPLY
    ack_ticket_id: str | None = None
    if ticket is not None:
        ack_ticket_id = ticket.id
        try:
            ticket_service.add_message(
                session,
                ticket.id,
                "system",
                "买家要求转回 AI 客服,工单已释放,由 AI 继续服务。",
                is_internal=True,
            )
            ticket_service.close(session, ticket.id)
            ack = f"好的,已结束人工服务,工单 #{ticket.id} 已关闭,由我继续为您服务。"
        except StateConflictError:
            # 并发场景:坐席刚好关单/状态已变,释放失败不影响"回 active"。
            session.rollback()
            ack = RESUME_AI_REPLY
    session_row.status = "active"
    record_event(
        session,
        trace_id=trace_id,
        event=EVENT_SESSION_STATUS,
        payload={
            "from": "handed_off",
            "to": "active",
            "reason": "buyer_resume_ai",
            "ticket_id": ack_ticket_id,
        },
    )
    session.flush()
    return TurnResult(
        intent="handoff_resume",
        confidence=1.0,
        intent_reason="买家主动要求转回 AI 客服",
        classification_path="route_resume",
        reply=RESUME_AI_REPLY,
        citations=[],
        pending_confirm=None,
        tool_calls=[],
        session_status="active",
        ai_replied=True,
        ack=ack,
        ack_ticket_id=ack_ticket_id,
    )
