"""nodes.py — W3-A 六节点(按"是否需要 LLM / 是否需要 IO"切分,见 G3 设计卡)。

节点边界(03§4.2.2 四条硬规则之一:节点只做一件事):

==================  ==========  ==================================================
节点                 是否需要  职责
                      LLM / IO
==================  ==========  ==================================================
``perceive``          纯读      归一化进线文本 + 注入一次买家长期记忆(只读,不写库)
``classify``          LLM        意图 + 槽位(高置信快捷路径 → FakeLLM),写 1 条分类事件
``retrieve``          IO         检索政策条款(policy__search,无命中确定性改写再查)
``act``               IO         经工具层执行业务读写;自环;写操作前走确认门 interrupt
``compose``           LLM        组装回复文案;任务终态才抽取偏好写记忆(MOCK,见 store)
``handoff``           IO         转人工:建工单确认卡 + 会话置 handed_off + 确认门
==================  ==========  ==================================================

确认门(P0-C2 铁律):LangGraph 在 resume 时会**把节点从头重跑**一遍,
所以"建确认卡(有副作用)"和"interrupt 收确认"必须落在两个不同的 super-step。
本模块用 ``state["confirm"]`` 做守卫:

1. 第一次进 ``act`` / ``handoff``:只 ``invoke`` 写工具拿到确认卡 → ``return``(落 checkpoint);
2. 路由去 ``compose`` 渲染"待确认"话术(挂起那一轮的 HTTP 响应必须带 reply);
3. 再次进 ``act`` / ``handoff`` 时才 ``interrupt``——此刻 interrupt 之前**没有任何副作用**,
   恢复重跑这一段是安全的;
4. resume 后 ``interrupt`` 返回人工决定,approved 才调 ``refund.execute`` /
   ``handoff.execute`` 真正执行(G7:确认后仍需幂等键)。

循环有上限:``steps > NODE_STEP_LIMIT`` 的第 9 圈不再调任何工具,直接转人工
(reason=``step_limit``),对应正例 ``ai_service.py:793-809`` 的 for-else 兜底。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utc_now
from app import guardrails
from app.graph.state import GraphState, NODE_STEP_LIMIT
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
from app.memory.injection import load_injection
from app.memory.store import extract_and_write
from app.models import ChatMessage, ChatSession, KnowledgeItem
from app.perception import normalize
from app.tools import ToolContext, get_confirm, invoke
from app.tools import handoff as handoff_tool
from app.tools import refund as refund_tool
from app.tools.results import ToolExecutionError
from app.trace import (
    EVENT_CLASSIFY,
    EVENT_QUERY_REWRITE,
    EVENT_SESSION_STATUS,
    record_event,
)

# ---------------------------------------------------------------------------
# 图事件(命名空间约定:模块__动作,复用 tool_events 表)
# ---------------------------------------------------------------------------

#: 第 9 圈强制转人工(超轮次兜底),payload 含 steps / limit / reason
EVENT_GRAPH_STEP_LIMIT = "graph__step_limit"
#: 确认门通过后真正执行了写工具(一次副作用只应有一条),payload 含 tool / 结果 id
EVENT_GRAPH_CONFIRM_EXECUTED = "graph__confirm_executed"

#: 写工具名(确认门按它选择 execute 函数)
TOOL_REFUND = "refund__create"
TOOL_HANDOFF = "handoff__create_ticket"

#: 高置信快捷路径:命中即 conf=1.0,跳过 LLM(只放"无需理解就能确定"的表达)。
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

#: 转接原因的客户可读文案(判定原因保留原始语义用于 trace)。
_REASON_LABELS: dict[str, str] = {
    "明示人工": "用户主动要求人工",
    "连续 3 轮同意图未解决": "连续 3 轮未解决",
    "金额争议:客户声称金额与订单实付不符": "金额争议:客户声称的退款金额与订单实付不符",
    "step_limit": "自动处理步数超限",
}

_ORDER_ID_RE = re.compile(r"\b(?:seed-order-|ORD-)[A-Za-z0-9-]{2,63}\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"(?:实付|付了|按|退我|退给)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?")

#: 确认卡四要素(对外 pending_confirm 的键,顺序与 G7 卡一致)。
_CARD_KEYS = ("what", "impact", "irreversibility", "alternatives")

#: 人工确认的肯定表达(恢复值可能是 str / bool / dict;无法识别时按拒绝处理)。
_APPROVE_WORDS = frozenset(
    {"approve", "approved", "yes", "y", "ok", "confirm", "confirmed", "true",
     "同意", "确认", "批准", "通过"}
)


# ---------------------------------------------------------------------------
# 纯函数工具(不碰 IO;与 W2-C 基线逐条对齐,保证话术与判定不漂移)
# ---------------------------------------------------------------------------


def text_of(state: GraphState) -> str:
    """取本轮归一化后的进线文本。"""
    return str((state.get("percept") or {}).get("text") or "")


def _shortcut_intent(text: str) -> str | None:
    for intent, hints in HIGH_CONFIDENCE_RULES:
        if any(h in text for h in hints):
            return intent
    return None


def _resolve_order_id(text: str, context: dict[str, Any]) -> str | None:
    """订单号来源:请求上下文(已解析的会话槽位)优先,其次消息文本中识别。"""
    return guardrails.extract_order_id(text, context)


def _text_guard(text: str, context: dict[str, Any]) -> dict[str, Any]:
    """FIX-3 确定性守卫画像(纯函数,便于单测)。

    ``hard`` = 技术型注入(SQL/命令/伪造工具调用)或隐私索取,整轮不应执行工具;
    ``suspect`` 只表示软命中(剥离片段后仍可处理正当诉求)。
    """
    profile = guardrails.detect_injection(text)
    has_order = bool(guardrails.extract_order_id(text, context))
    privacy = guardrails.is_privacy_probe(text, has_order)
    return {"profile": profile, "privacy": privacy, "hard": bool(profile["hard"])}


def _claim_evidence(state: GraphState, confirm: dict[str, Any] | None) -> dict[str, bool]:
    """FIX-4:组装"状态声称"所需证据(全部来自本轮 state/trace,不查模型)。"""
    observations = list(state.get("observations") or [])
    verdict = dict(state.get("verdict") or {})
    executed = dict((confirm or {}).get("executed") or {})
    executed_ok = bool(executed.get("ok"))
    tool = (confirm or {}).get("tool")
    handoff_done = bool(executed_ok and tool == TOOL_HANDOFF)
    refund_done = bool(executed_ok and tool == TOOL_REFUND)
    lookup_failed = any(
        str(item.get("name") or "") == "shop__get_order" and not item.get("ok")
        for item in observations
    )
    return {
        "handoff_done": handoff_done,
        "refund_done": refund_done,
        "address_done": False,
        "lookup_denied": bool(lookup_failed or verdict.get("kind") == "order_not_found"),
        "no_hit": bool(verdict.get("no_hit")),
        "write_done": handoff_done or refund_done,
    }


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
        except ValueError:  # pragma: no cover - 正则只放数字
            continue
    return amounts


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


def _rewrite_query(text: str) -> str | None:
    for hints, rewritten in QUERY_REWRITES:
        if any(h in text for h in hints):
            return rewritten
    return None


def _friendly_reason(reason: str) -> str:
    """把内部判定原因翻成客户可读的转接原因。"""
    return _REASON_LABELS.get(reason, reason)


def _order_summary(order: dict[str, Any] | None) -> dict[str, Any] | None:
    """订单摘要(只留 3 个标量,不把工具完整返回体塞进 State)。"""
    if not order:
        return None
    return {
        "order_id": order.get("order_id"),
        "status": order.get("status"),
        "total_cents": int(order.get("total_cents") or 0),
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


def _call_record(name: str, result: Any, note: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "ok": bool(result.ok),
        "error_code": getattr(result.error, "code", None) if not result.ok else None,
    }
    if note:
        record["note"] = note
    return record


def _parse_decision(raw: Any) -> bool:
    """把 resume 传入的确认决定归一化成布尔(无法识别 → 拒绝,失败要 fail-closed)。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, dict):
        for key in ("approved", "confirm", "decision", "answer"):
            if key in raw:
                return _parse_decision(raw[key])
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in _APPROVE_WORDS
    return False


def _card_of(pending: dict[str, Any]) -> dict[str, str]:
    return {key: str(pending.get(key) or "") for key in _CARD_KEYS}


# ---------------------------------------------------------------------------
# 条件边(放在 nodes 里,方便单测;build.py 只做装配)
# ---------------------------------------------------------------------------


def needs_more_steps(state: GraphState) -> str:
    """``act`` 之后:继续自环 / 去 compose / 去 handoff。

    返回 ``"more"``(act 自环)、``"done"``(compose)或 ``"handoff"``。
    """
    if state.get("handoff"):
        return "handoff"
    if (state.get("intent") or {}).get("name") == INTENT_HANDOFF:
        return "handoff"
    confirm = state.get("confirm")
    if confirm and (not confirm.get("replied") or confirm.get("decided")):
        # 刚建完卡 → 先 compose 出"待确认"话术;刚执行完 → compose 出终态话术
        return "done"
    if state.get("plan"):
        return "more"
    return "done"


def after_compose(state: GraphState) -> str:
    """``compose`` 之后:待确认 → 回到确认门所在节点;否则结束。"""
    confirm = state.get("confirm")
    if confirm and not confirm.get("decided"):
        return "act" if confirm.get("tool") == TOOL_REFUND else "handoff"
    return "end"


def _next_plan(state: GraphState) -> list[dict[str, Any]]:
    """根据意图与已完成调用,算出"下一批"待执行工具调用(空列表表示已收敛)。

    放在模块级(纯函数,无 IO)便于测试用 monkeypatch 构造超轮次场景。
    """
    intent = (state.get("intent") or {}).get("name")
    observations = list(state.get("observations") or [])
    done = {str(o.get("name")) for o in observations if o.get("ok")}
    verdict = state.get("verdict") or {}
    order_id = (state.get("intent") or {}).get("order_id")

    if intent == INTENT_ORDER_JUDGE:
        if not order_id or verdict.get("kind") in ("order_missing_id", "order_not_found"):
            return []
        if "shop__get_order" not in done:
            return [{"name": "shop__get_order", "args": {"order_id": order_id}}]
        if state.get("handoff") or verdict.get("kind") == "handoff":
            return []
        if "policy__check_return" not in done:
            return []  # 参数依赖 get_order 结果,由 _after_tool 精确补入
        return []

    if intent == INTENT_HANDOFF:
        if order_id and "shop__get_order" not in done:
            return [{"name": "shop__get_order", "args": {"order_id": order_id}}]
        return []

    return []


# ---------------------------------------------------------------------------
# 节点工厂:把 session / session_row / llm 绑进闭包
# ---------------------------------------------------------------------------


def make_nodes(
    *,
    session: Session,
    session_row: ChatSession,
    llm: LLMClient | None = None,
    prior_intent_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """构造六节点。``prior_intent_counts`` 是 runner 预取的"本会话历史同意图轮数"。"""
    llm = llm or FakeLLM()
    merchant_id = session_row.merchant_id

    # ---- 内部小工具 ------------------------------------------------------

    def _ctx(trace_id: str) -> ToolContext:
        return ToolContext(
            session=session,
            merchant_id=merchant_id,
            customer_id=session_row.customer_id,
            trace_id=trace_id,
        )

    def _clause(clause_id: Any) -> dict[str, Any] | None:
        """按 clause_id 取知识库原文(引用必须真实存在,查不到就不引用)。"""
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

    def _render(facts: dict[str, Any]) -> str:
        """让 LLM 产出回复文本(MOCK: FakeLLM 模板);失败时返回安全兜底文案。"""
        messages = [
            {
                "role": "system",
                "content": (
                    "你是售后客服,用中文回复,不要编造政策。"
                    "引用政策条款时必须逐字包含条款里的关键数字短语(金额/天数/运费/比例),不得改写或四舍五入;"
                    "不得声称已完成转账、退款、改地址、建单、转人工等操作,除非本轮事实里明确给出执行结果;"
                    "尚未确认的动作只能说\"将为您…(待确认)\"。"
                ),
            },
            build_facts_message(facts),
        ]
        try:
            raw = llm.complete(messages, SCHEMA_REPLY)
            reply = json.loads(raw).get("reply", "")
        except (json.JSONDecodeError, AttributeError):  # pragma: no cover
            reply = ""
        return reply or "抱歉,我暂时无法处理该问题,可以为您转接人工客服。"

    def _prior_same_intent(intent: str) -> int:
        if prior_intent_counts is not None:
            return int(prior_intent_counts.get(intent) or 0)
        rows = session.scalars(
            select(ChatMessage.intent).where(
                ChatMessage.session_id == session_row.id,
                ChatMessage.role == "user",
                ChatMessage.intent == intent,
            )
        ).all()
        return len(rows)

    def _confirm_args(token: str) -> dict[str, Any]:
        """从确认卡里取"真正执行"的参数(handler 归一化后的 args,不是原始入参)。"""
        if not token:
            return {}
        request = get_confirm(session, token)
        if request is None:
            return {}
        try:
            return dict(json.loads(request.payload).get("args") or {})
        except (TypeError, ValueError):  # pragma: no cover - payload 由本模块写
            return {}

    def _build_confirm(tool: str, pending: dict[str, Any]) -> dict[str, Any] | None:
        token = str(pending.get("confirm_token") or "")
        if not token:
            return None
        return {
            "tool": tool,
            "args": _confirm_args(token),
            "card": _card_of(pending),
            "token": token,
            "replied": False,
            "decided": False,
            "approved": None,
            "executed": None,
        }

    def _execute_confirmed(
        ctx: ToolContext, confirm: dict[str, Any]
    ) -> dict[str, Any]:
        """确认门通过后真正执行写工具——**全图唯一产生业务副作用的地方**。

        幂等:refund 由工具层按 (order_id, reason, amount_cents) 算幂等键;
        confirm 卡状态置 confirmed,重复 resume 不会再次执行。
        """
        token = str(confirm.get("token") or "")
        request = get_confirm(session, token)
        if request is None:
            return {"ok": False, "data": {},
                    "error": {"code": "CONFIRM_NOT_FOUND", "message": "确认卡不存在或已被清理"}}
        if request.status != "pending":
            return {"ok": False, "data": {},
                    "error": {"code": "CONFIRM_NOT_PENDING",
                              "message": f"确认卡状态为 {request.status},不再执行"}}
        expires_at = request.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at is not None and expires_at < utc_now():
            request.status = "expired"
            session.flush()
            return {"ok": False, "data": {},
                    "error": {"code": "CONFIRM_EXPIRED", "message": "确认超时(10 分钟),已按拒绝处理"}}

        args = _confirm_args(token)
        tool = str(confirm.get("tool") or "")
        try:
            if tool == TOOL_REFUND:
                data = refund_tool.execute(args, ctx)
            elif tool == TOOL_HANDOFF:
                data = handoff_tool.execute(args, ctx)
            else:  # pragma: no cover - 只有两个写工具
                return {"ok": False, "data": {},
                        "error": {"code": "UNKNOWN_TOOL", "message": f"未知写工具:{tool}"}}
        except ToolExecutionError as exc:
            return {"ok": False, "data": {}, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001 - 执行异常统一收敛为"结果未知"
            return {"ok": False, "data": {}, "error": {"code": "UNKNOWN_OUTCOME", "message": str(exc)}}

        request.status = "confirmed"
        session.flush()
        return {"ok": True, "data": dict(data), "error": {}}

    # ---- ① perceive:纯读(无 LLM / 不写库),任务开始注入一次记忆 ----------

    def perceive(state: GraphState) -> dict[str, Any]:
        """归一化 + 任务开始时注入一次长期记忆;不调 LLM、不写库、不写 trace。"""
        percept = normalize(text_of(state))
        # 任务开始只读一次记忆(固定顺序,空记忆显式"无历史偏好");隔离项查不到
        memory = load_injection(
            session,
            merchant_id=merchant_id,
            user_id=session_row.customer_id,
        )
        return {"percept": percept.model_dump(), "memory": memory}

    # ---- ② classify:只调 LLM + 写一条分类事件 ---------------------------

    def classify(state: GraphState) -> dict[str, Any]:
        """意图 + 槽位:高置信快捷路径优先,其余交给 LLM;历史同意图 3 轮升转人工。"""
        trace_id = str(state.get("trace_id") or "")
        raw_text = text_of(state)
        context = dict(state.get("context") or {})
        order_id = _resolve_order_id(raw_text, context)
        guard = _text_guard(raw_text, context)
        profile = guard["profile"]
        # FIX-3:注入命中只影响"理解",不把注入片段送进模型;软命中剥离后继续处理正当诉求
        text = guardrails.strip_injection(raw_text) if profile["suspect"] else raw_text
        facts = {
            "text": text,
            "has_order": bool(order_id),
            "unresolved_rounds": int(context.get("unresolved_rounds") or 0),
        }

        blocked = bool(guard["hard"] or guard["privacy"])
        shortcut = None if blocked else _shortcut_intent(text)
        if guard["privacy"]:
            decision: dict[str, Any] = {
                "intent": INTENT_OTHER,
                "confidence": 1.0,
                "reason": "隐私索取拦截",
            }
            path = "guard:privacy_block"
        elif guard["hard"]:
            decision = {
                "intent": INTENT_OTHER,
                "confidence": 1.0,
                "reason": "注入指令拦截",
            }
            path = "guard:injection_block"
        elif shortcut is not None:
            decision = {
                "intent": shortcut,
                "confidence": 1.0,
                "reason": "明示人工",
            }
            path = "fast_path"
        else:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是售后客服的意图分类器,只输出 JSON。"
                        "示例:用户说“我 M 码偏紧,以后都给我推荐 L 码” → 这是偏好/记忆场景,"
                        "输出 other,不要归政策问答。"
                    ),
                },
                {"role": "user", "content": text},
                build_facts_message(facts),
            ]
            raw = llm.complete(messages, SCHEMA_INTENT)
            try:
                decision = json.loads(raw)
            except json.JSONDecodeError:  # pragma: no cover - 规则表不会走到
                decision = {"intent": INTENT_OTHER, "confidence": 0.0, "reason": "解析失败"}
            if decision.get("intent") not in {
                INTENT_POLICY_QA, INTENT_ORDER_JUDGE, INTENT_HANDOFF, INTENT_OTHER,
            }:
                decision["intent"] = INTENT_OTHER
            path = "llm"
            # FIX-3:伪造政策的诉求(ADV-010)不交给模型选路,强制走政策检索核对原文
            if "policy_forgery" in profile["kinds"]:
                decision = {
                    "intent": INTENT_POLICY_QA,
                    "confidence": 0.9,
                    "reason": "政策争议:核对政策原文",
                }
                path = "guard:policy_dispute"

        # 连续 3 轮同意图未解决 → 升级转人工(消费历史消息,不靠关键词"三遍")
        if not blocked and decision.get("intent") != INTENT_HANDOFF and _prior_same_intent(decision["intent"]) + 1 >= 3:
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
        return {
            "intent": {
                "name": decision["intent"],
                "confidence": float(decision.get("confidence") or 0.0),
                "reason": str(decision.get("reason") or ""),
                "path": path,
                "order_id": order_id,
            }
        }

    # ---- ③ retrieve:只做检索 IO -----------------------------------------

    def retrieve(state: GraphState) -> dict[str, Any]:
        """政策问答的检索:命中取前 3 条条款;无命中按确定性改写再查一次。"""
        updates: dict[str, Any] = {"retrieved": []}
        if (state.get("intent") or {}).get("name") != INTENT_POLICY_QA:
            return updates

        trace_id = str(state.get("trace_id") or "")
        text = text_of(state)
        ctx = _ctx(trace_id)
        result = invoke("policy__search", {"query": text}, ctx)
        observations = list(state.get("observations") or [])
        observations.append(_call_record("policy__search", result))

        if result.ok and result.data.get("no_hit"):
            rewritten = _rewrite_query(text)
            if rewritten:
                record_event(
                    session,
                    trace_id=trace_id,
                    event=EVENT_QUERY_REWRITE,
                    payload={"original": text, "rewritten": rewritten,
                             "strategy": "keyword_map(MOCK)"},
                )
                retry = invoke("policy__search", {"query": rewritten}, ctx)
                observations.append(
                    _call_record("policy__search", retry, note=f"改写检索:{rewritten}")
                )
                if retry.ok and not retry.data.get("no_hit"):
                    result = retry

        if not result.ok:
            return {
                "retrieved": [],
                "observations": observations,
                "verdict": {"kind": "retrieval_unavailable"},
            }

        no_hit = bool(result.data.get("no_hit"))
        clauses = [] if no_hit else [dict(c) for c in (result.data.get("results") or [])[:3]]
        return {
            "retrieved": clauses,
            "observations": observations,
            "verdict": {"kind": "policy_answer", "no_hit": no_hit},
        }

    # ---- ④ act:业务 IO + 自环 + 确认门 ----------------------------------

    def act(state: GraphState) -> dict[str, Any]:
        """每圈只执行一个工具调用;写操作先建卡,再在下一圈 interrupt 收确认。"""
        trace_id = str(state.get("trace_id") or "")
        ctx = _ctx(trace_id)
        steps = int(state.get("steps") or 0)

        # ---- 确认门:interrupt 之前没有任何副作用(恢复时本节点从头重跑) ----
        # 放在圈数自增之前:resume 只是重跑同一圈,不该再计一步、更不该触发熔断。
        confirm = state.get("confirm")
        if confirm and not confirm.get("decided"):
            decision = interrupt(
                {
                    "kind": "confirm",
                    "tool": confirm.get("tool"),
                    "confirm_token": confirm.get("token"),
                    "card": confirm.get("card"),
                    "prompt": "该操作会写入业务数据,请人工确认后继续",
                }
            )
            approved = _parse_decision(decision)
            if not approved:
                return {
                    "steps": steps,
                    "confirm": {**confirm, "decided": True, "approved": False},
                    "verdict": {"kind": "confirm_denied", "tool": confirm.get("tool")},
                }
            executed = _execute_confirmed(ctx, confirm)
            if executed.get("ok"):
                record_event(
                    session,
                    trace_id=trace_id,
                    event=EVENT_GRAPH_CONFIRM_EXECUTED,
                    payload={
                        "tool": confirm.get("tool"),
                        "confirm_token": confirm.get("token"),
                        "result_id": (executed.get("data") or {}).get("refund_id"),
                        "idempotent_hit": (executed.get("data") or {}).get("idempotent_hit"),
                    },
                )
            return {
                "steps": steps,
                "confirm": {**confirm, "decided": True, "approved": True, "executed": executed},
                "verdict": {
                    "kind": "confirm_approved",
                    "tool": confirm.get("tool"),
                    "executed": executed,
                },
            }

        # ---- FIX-3:硬注入/隐私索取整轮零工具、零写入(EDGE-008/ADV-012/014) ----
        guard = _text_guard(text_of(state), dict(state.get("context") or {}))
        if guard["privacy"]:
            return {"steps": steps, "plan": [], "verdict": {"kind": "privacy_block"}}
        if guard["hard"]:
            return {"steps": steps, "plan": [], "verdict": {"kind": "security_block"}}

        steps += 1

        # ---- 熔断:第 NODE_STEP_LIMIT + 1 圈不再调工具,直接转人工 ----
        if steps > NODE_STEP_LIMIT:
            record_event(
                session,
                trace_id=trace_id,
                event=EVENT_GRAPH_STEP_LIMIT,
                payload={
                    "steps": steps,
                    "limit": NODE_STEP_LIMIT,
                    "reason": "step_limit",
                    "tool_calls": len(state.get("observations") or []),
                },
            )
            return {
                "steps": steps,
                "plan": [],
                "handoff": True,
                "verdict": {
                    "kind": "step_limit",
                    "reason": "step_limit",
                    "handoff_reason": "step_limit",
                    "steps": steps,
                    "detail": f"执行 {NODE_STEP_LIMIT} 圈仍未收敛,强制转人工",
                },
            }

        # ---- 确认门:interrupt 之前没有任何副作用(恢复时本节点从头重跑) ----
        plan = list(state.get("plan") or [])
        observations = list(state.get("observations") or [])
        # ---- FIX-1:订单核验强制链(服务端守卫,不依赖模型是否"先查单") ----
        text = text_of(state)
        context = dict(state.get("context") or {})
        intent_name = (state.get("intent") or {}).get("name")
        if guardrails.needs_order_verification(
            text=text,
            context=context,
            intent_name=intent_name,
            observations=observations,
        ):
            forced_order_id = guardrails.extract_order_id(text, context)
            if not forced_order_id:
                # 抽取不到订单号:强制走追问话术,绝不臆测订单
                return {
                    "steps": steps,
                    "plan": [],
                    "verdict": {"kind": "order_missing_id"},
                }
            plan = [{"name": "shop__get_order", "args": {"order_id": forced_order_id}}]
        if not plan:
            plan = _next_plan(state)
        if not plan:
            # 本轮无事可做:policy 已由 retrieve 完成 / 判定已收敛 / 无订单号
            return {"steps": steps, "plan": []}

        call = plan[0]
        rest = plan[1:]
        result = invoke(str(call["name"]), call.get("args") or {}, ctx)
        observations.append(_call_record(str(call["name"]), result, call.get("note")))
        updates: dict[str, Any] = {"steps": steps, "plan": rest, "observations": observations}
        updates.update(_after_tool(state, str(call["name"]), result, rest))
        return updates

    def _after_tool(
        state: GraphState,
        name: str,
        result: Any,
        rest: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """工具调用后的判定收尾:算下一步、判定事实、确认卡。"""
        intent = (state.get("intent") or {}).get("name")
        verdict = dict(state.get("verdict") or {})
        context = dict(state.get("context") or {})
        text = text_of(state)

        if name == "shop__get_order":
            if intent == INTENT_HANDOFF:
                if result.ok:
                    order = _order_summary(result.data)
                    return {"plan": [], "verdict": {**verdict, "kind": "handoff",
                                                    "order": order,
                                                    "order_id": (order or {}).get("order_id")}}
                # 查不到/越权:不要把订单号塞进工单,避免二次失败
                return {"plan": [], "verdict": {**verdict, "kind": "handoff",
                                                "order": None, "order_id": None}}
            if not result.ok:
                return {"plan": [], "retrieved": [], "verdict": {"kind": "order_not_found"}}
            # FIX-1:非订单意图(如"帮我查订单地址")只核验、不自动进入退款判责链
            if intent not in (INTENT_ORDER_JUDGE, INTENT_HANDOFF):
                checked_order = _order_summary(result.data)
                return {
                    "plan": [],
                    "verdict": {
                        **verdict,
                        "kind": "order_checked",
                        "order": checked_order,
                        "order_id": (checked_order or {}).get("order_id"),
                    },
                }
            order = dict(result.data or {})
            total_cents = int(order.get("total_cents") or 0)
            claimed = _claimed_amounts_cents(text, context)
            if _has_risk(text) and claimed and any(c != total_cents for c in claimed):
                return {
                    "plan": [],
                    "handoff": True,
                    "verdict": {
                        "kind": "handoff",
                        "handoff_reason": "金额争议:客户声称金额与订单实付不符",
                        "order": _order_summary(order),
                        "order_id": order.get("order_id"),
                    },
                }
            reason_text = _extract_reason(text)
            days = _days_since_delivery(context, order.get("created_at"))
            return {
                "plan": [{
                    "name": "policy__check_return",
                    "args": {
                        "order_status": order.get("status"),
                        "reason": reason_text,
                        "days_since_delivery": days,
                    },
                }],
                "verdict": {
                    "kind": "order_checked",
                    "order": _order_summary(order),
                    "order_id": order.get("order_id"),
                    "reason_text": reason_text,
                },
            }

        if name == "policy__check_return":
            checked = (result.data or {}) if result.ok else {}
            clause = _clause(checked.get("clause_id"))
            retrieved = [clause] if clause else []
            order_id = verdict.get("order_id")
            status = (verdict.get("order") or {}).get("status")
            if result.ok and checked.get("eligible"):
                amount = int(
                    context.get("refund_amount_cents")
                    or (verdict.get("order") or {}).get("total_cents")
                    or 0
                )
                return {
                    "retrieved": retrieved,
                    "plan": [{
                        "name": TOOL_REFUND,
                        "args": {
                            "order_id": order_id,
                            "amount_cents": amount,
                            "reason": verdict.get("reason_text") or "七天无理由",
                        },
                    }],
                    "verdict": {
                        "kind": "order_verdict_eligible",
                        "order_id": order_id,
                        "status": status,
                        "amount_cents": amount,
                        "reason_text": verdict.get("reason_text"),
                        "verdict_reason": checked.get("reason"),
                    },
                }
            return {
                "retrieved": retrieved,
                "plan": [],
                "verdict": {
                    "kind": "order_verdict_refused",
                    "order_id": order_id,
                    "status": status,
                    "verdict_reason": checked.get("reason") or "未能完成退换货判定",
                },
            }

        if name == TOOL_REFUND:
            if not result.ok:
                return {"plan": [], "verdict": {**verdict, "kind": "refund_create_failed"}}
            pending = dict((result.data or {}).get("pending_confirm") or {})
            return {"plan": [], "confirm": _build_confirm(TOOL_REFUND, pending)}

        return {}

    # ---- ⑤ compose:组装文案 + 终态抽取记忆(不调工具 / 不写事件) ---------

    def compose(state: GraphState) -> dict[str, Any]:
        verdict = dict(state.get("verdict") or {})
        confirm = state.get("confirm")
        if confirm and confirm.get("decided"):
            out = {"confirm": confirm, "draft": _final_reply(confirm)}
        else:
            draft = _reply_for(verdict, state)
            retrieved = list(state.get("retrieved") or [])
            # FIX-4:状态声称必须有本轮证据,无证据逐句替换为诚实话术。
            # 有条款原文的回复不在此处改写(避免把条款里的"已受理"等正文误判为声称),
            # 其引用保真由 FIX-5 的确定性拼接负责。
            if not retrieved:
                draft, _replaced = guardrails.audit_status_claims(
                    draft, _claim_evidence(state, confirm)
                )
            # FIX-5:引用保真——条款里的关键数字短语必须在回复中逐字出现,
            # 否则确定性追加条款原文(不回炉模型,避免二次漂移)。
            if retrieved and verdict.get("kind") in (
                "policy_answer",
                "order_verdict_eligible",
                "order_verdict_refused",
            ):
                draft, _missing = guardrails.ensure_citation_fidelity(draft, retrieved)
            if verdict.get("kind") == "policy_answer":
                draft = guardrails.ensure_policy_word(draft)
            # FIX-4:转人工确认卡未确认前,统一"将为您转接人工(待确认)"口径。
            if confirm and not confirm.get("decided") and confirm.get("tool") == TOOL_HANDOFF:
                reason_label = str(verdict.get("reason") or "用户主动要求人工")
                order_id = verdict.get("order_id") or (state.get("intent") or {}).get("order_id")
                suffix = f"(订单 {order_id})" if order_id else ""
                draft = (
                    f"将为您转接人工(待确认):转接原因「{reason_label}」{suffix}。"
                    "请回复“确认”后,我再为您创建人工工单。"
                )
            if confirm and not confirm.get("replied"):
                out = {"draft": draft, "confirm": {**confirm, "replied": True}}
            else:
                out = {"draft": draft}
        # 任务结束(图终态)才抽取偏好写记忆;act 自环/待确认挂起轮一律不写
        if not (confirm and not confirm.get("decided")):
            guard = _text_guard(text_of(state), dict(state.get("context") or {}))
            # FIX-3:注入/隐私拦截轮不写记忆;混合文本只喂剥离后的正当诉求
            if not (guard["hard"] or guard["privacy"]):
                extract_and_write(
                    session,
                    merchant_id=merchant_id,
                    user_id=session_row.customer_id,
                    text=guardrails.strip_injection(text_of(state)),
                    llm=llm,
                )
        return out

    def _reply_for(verdict: dict[str, Any], state: GraphState) -> str:
        """按判定事实包选择模板(FakeLLM 的 facts["kind"] 契约与 W2-C 完全一致)。"""
        kind = verdict.get("kind")
        retrieved = list(state.get("retrieved") or [])
        clause = retrieved[0] if retrieved else None
        if kind == "policy_answer":
            return _render(
                {"kind": "policy_answer", "no_hit": bool(verdict.get("no_hit")), "clauses": retrieved}
            )
        if kind == "retrieval_unavailable":
            return "政策检索服务暂时不可用,请稍后重试或转人工客服。"
        if kind == "security_block":
            return (
                "为保障账户与数据安全,该请求包含无法执行的操作指令,已停止处理。"
                "如需售后,请直接描述您的订单问题。"
            )
        if kind == "privacy_block":
            return (
                "抱歉,我无法提供其他客户的隐私信息。"
                "如需查询您自己的订单,请提供订单号。"
            )
        if kind == "order_missing_id":
            return _render({"kind": "order_missing_id"})
        if kind == "order_not_found":
            # FIX-1:越权/不存在统一口径,由工具返回的错误码驱动,不臆造订单信息。
            return (
                "未能在当前会话下找到您提到的订单。请确认订单号是否正确;"
                "若该订单不属于当前账号,我无法展示其信息。"
            )
        if kind == "order_verdict_eligible":
            return _render({
                "kind": "order_verdict_eligible",
                "order_id": verdict.get("order_id"),
                "status": verdict.get("status"),
                "amount_cents": verdict.get("amount_cents"),
                "reason_text": verdict.get("reason_text"),
                "verdict_reason": verdict.get("verdict_reason"),
                "clause": clause,
            })
        if kind == "order_verdict_refused":
            return _render({
                "kind": "order_verdict_refused",
                "order_id": verdict.get("order_id"),
                "status": verdict.get("status"),
                "verdict_reason": verdict.get("verdict_reason"),
                "clause": clause,
            })
        if kind == "refund_create_failed":
            return "退款申请未能创建(工具返回失败),请稍后重试或转人工客服。"
        if kind == "handoff":
            return _render({
                "kind": "handoff",
                "reason": verdict.get("reason"),
                "order_id": verdict.get("order_id"),
            })
        if kind == "step_limit":
            return "您的问题需要更多步骤处理,已为您转接人工客服继续跟进。"
        if kind == "order_checked":
            order = dict(verdict.get("order") or {})
            return (
                f"已核验订单 {order.get('order_id')},当前状态:{order.get('status')}。"
                "如需办理退换货,请说明具体诉求。"
            )
        # FIX-7:偏好/记忆场景也把注入的记忆交给回复层,确保引用来源可追溯。
        return _render({"kind": "other", "memory": state.get("memory") or {}})

    def _final_reply(confirm: dict[str, Any]) -> str:
        """确认门之后的终态话术(确定性模板,避免 MOCK LLM 出无关文案)。"""
        tool = confirm.get("tool")
        if not confirm.get("approved"):
            if tool == TOOL_HANDOFF:
                return "已取消本次人工工单的创建,未执行任何变更。如需转人工,请直接回复“转人工”。"
            return "已取消本次退款申请,未执行任何变更。如仍需办理,可重新发起或转人工客服。"
        executed = dict(confirm.get("executed") or {})
        if not executed.get("ok"):
            error = dict(executed.get("error") or {})
            code = error.get("code") or "UNKNOWN"
            return f"确认已收到,但执行未能完成({code});已保留确认记录,请稍后重试或转人工客服。"
        data = dict(executed.get("data") or {})
        if tool == TOOL_REFUND:
            amount = int(data.get("amount_cents") or 0)
            extra = "(命中幂等键,复用已有退款单,未重复创建)" if data.get("idempotent_hit") else ""
            return (
                f"已按您的确认提交退款申请:退款单号 {data.get('refund_id')},"
                f"金额 ¥{amount / 100:.2f}({amount} 分),状态 {data.get('status')}{extra}。"
                "审核通过后退款将原路到账,请留意通知。"
            )
        return (
            f"已按您的确认创建人工工单:工单号 {data.get('ticket_id')},"
            f"状态 {data.get('status')},坐席会尽快跟进。在人工接手前,我先不再自动回复。"
        )

    # ---- ⑥ handoff:转人工 IO + 确认门 -----------------------------------

    def handoff(state: GraphState) -> dict[str, Any]:
        """建工单确认卡 + 会话置 handed_off;下一圈再 interrupt 收确认后 execute。"""
        trace_id = str(state.get("trace_id") or "")
        ctx = _ctx(trace_id)

        confirm = state.get("confirm")
        if confirm and not confirm.get("decided"):
            decision = interrupt(
                {
                    "kind": "confirm",
                    "tool": confirm.get("tool"),
                    "confirm_token": confirm.get("token"),
                    "card": confirm.get("card"),
                    "prompt": "创建工单会写入业务数据并转交人工,请确认后继续",
                }
            )
            approved = _parse_decision(decision)
            verdict = dict(state.get("verdict") or {})
            if not approved:
                return {
                    "handoff": True,
                    "confirm": {**confirm, "decided": True, "approved": False},
                    "verdict": {**verdict, "kind": "confirm_denied", "tool": TOOL_HANDOFF},
                }
            executed = _execute_confirmed(ctx, confirm)
            if executed.get("ok"):
                record_event(
                    session,
                    trace_id=trace_id,
                    event=EVENT_GRAPH_CONFIRM_EXECUTED,
                    payload={
                        "tool": TOOL_HANDOFF,
                        "confirm_token": confirm.get("token"),
                        "result_id": (executed.get("data") or {}).get("ticket_id"),
                    },
                )
            return {
                "handoff": True,
                "confirm": {**confirm, "decided": True, "approved": True, "executed": executed},
                "verdict": {**verdict, "kind": "confirm_approved", "tool": TOOL_HANDOFF,
                            "executed": executed},
            }

        # ---- 首次进入:建工单确认卡(副作用只在工具层,且卡尚未执行) ----
        verdict = dict(state.get("verdict") or {})
        intent = dict(state.get("intent") or {})
        raw_reason = str(
            verdict.get("handoff_reason")
            or verdict.get("reason")
            or intent.get("reason")
            or "明示人工"
        )
        # FIX-6:入库/工具参数只接受 canonical 枚举;自然语言只进 summary。
        reason_enum = guardrails.map_handoff_reason(raw_reason)
        label = guardrails.handoff_reason_label(reason_enum)
        order = verdict.get("order") or None
        order_id = verdict.get("order_id", intent.get("order_id"))
        summary = _handoff_summary(
            reason=label, order=order, text=text_of(state),
            context=dict(state.get("context") or {}),
        )
        ticket = invoke(
            TOOL_HANDOFF,
            {"reason": reason_enum, "summary": summary, "order_id": order_id},
            ctx,
        )
        observations = list(state.get("observations") or [])
        observations.append(_call_record(TOOL_HANDOFF, ticket))

        built: dict[str, Any] | None = None
        if ticket.ok:
            previous = session_row.status
            session_row.status = "handed_off"
            record_event(
                session,
                trace_id=trace_id,
                event=EVENT_SESSION_STATUS,
                payload={"from": previous, "to": "handed_off", "reason": label},
            )
            built = _build_confirm(
                TOOL_HANDOFF, dict((ticket.data or {}).get("pending_confirm") or {})
            )

        return {
            "handoff": True,
            "confirm": built,
            "observations": observations,
            "verdict": {**verdict, "kind": "handoff", "reason": label,
                        "order_id": order_id, "handoff_reason": reason_enum},
        }

    return {
        "perceive": perceive,
        "classify": classify,
        "retrieve": retrieve,
        "act": act,
        "compose": compose,
        "handoff": handoff,
    }
