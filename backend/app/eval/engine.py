"""engine.py — 单条评测用例执行器。

职责(任务书 W4-A 第三层):把一条 case 真的跑一遍图,采集判定所需的**全部证据**,
交给 hard_assert(硬断言)与 judge(三维裁判)使用。

为什么是"临时 DB + 定向清理"而不是事务回滚:
- 图内的工具(``registry.invoke`` / ``refund_tool.execute`` / ``handoff_tool.execute``)
  会自行 ``session.commit()``,外层事务的 ``rollback()`` 无法撤销已提交的写;
- 因此每条 attempt 使用独立的临时 SQLite 文件,+ 每个 attempt 结束后定向删除
  **运行时表**(会话/消息/确认卡/事件/记忆/退款/非 seed 工单),保留 seed 订单与政策。

本模块不接真实 LLM:默认注入 ``FakeLLM``(MOCK),接口按 ``LLMClient`` 抽象留好 W4-C
替换点(``EvalEngine(llm=...)``)。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import create_db_engine
from app.graph import runner
from app.llm.base import FakeLLM, LLMClient
from app.memory.models import Memory
from app.models import (
    Base,
    ChatMessage,
    ChatSession,
    Order,
    RefundRequest,
    Ticket,
    TicketMessage,
)
from app.perception import PerceptionError, normalize
from app.trace import new_trace_id
from app.tools.events import ConfirmRequest, ToolEvent

from app.eval.validate_cases import TOOL_NAMES

#: 评测固定商户(seed 数据)
MERCHANT_ID = "seed-merchant-1"

#: 默认客户(既有 w1 用例的默认上下文)
DEFAULT_CUSTOMER_ID = "seed-cust-001"

#: 转人工后追加的探针消息:用于断言"AI 已停止自动回复"
DEFAULT_FOLLOWUP = "人工客服在吗？"

#: seed 工单 id 前缀(清理时保留)
SEED_TICKET_PREFIX = "seed-ticket-%"

#: MOCK 计量口径,与 backend/app/api/chat.py 完全一致
_CHARS_PER_TOKEN = 2
_PRICE_IN_PER_MTOK = 0.15
_PRICE_OUT_PER_MTOK = 0.60


def _tokens(chars: int) -> int:
    return max(1, (max(0, chars) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def _usage(text_in: str, text_out: str) -> dict[str, Any]:
    """单轮 MOCK 用量(与 api/chat.py 同口径)。"""
    tokens_in = _tokens(len(text_in))
    tokens_out = _tokens(len(text_out))
    cost = (tokens_in * _PRICE_IN_PER_MTOK + tokens_out * _PRICE_OUT_PER_MTOK) / 1_000_000
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 8),
        "estimated": True,
    }


def _sum_usage(items: list[dict[str, Any]]) -> dict[str, Any]:
    tokens_in = sum(int(i.get("tokens_in") or 0) for i in items)
    tokens_out = sum(int(i.get("tokens_out") or 0) for i in items)
    cost = sum(float(i.get("cost_usd") or 0.0) for i in items)
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 8),
        "estimated": True,
    }


#: 确认执行失败时图会把错误码写进话术(而不是抛异常);评测从这里回收错误码
_ERROR_CODES = ("BUSINESS_REJECT", "NOT_FOUND_OR_FORBIDDEN", "CONFIRM_NOT_PENDING")


def _error_code_in_reply(text: str) -> str | None:
    for code in _ERROR_CODES:
        if code in (text or ""):
            return code
    return None


def _jsonable(value: Any) -> Any:
    """payload 是 JSON 字符串;解析失败时原样保留(便于排错)。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class EvalEngine:
    """评测执行器:持有一个临时数据库 + 图执行所需参数。"""

    def __init__(
        self,
        *,
        db_path: str | Path,
        checkpoint_dir: str | Path,
        llm: LLMClient | None = None,
        merchant_id: str = MERCHANT_ID,
        setup_db: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.merchant_id = merchant_id
        self.llm: LLMClient = llm or FakeLLM()
        self.engine = create_db_engine(f"sqlite:///{self.db_path.as_posix()}")
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        if setup_db:
            Base.metadata.create_all(self.engine)
            self._seed()
            with self.Session() as session:
                self._seed_orders = self._snapshot_orders(session)

    # ------------------------------------------------------------------
    # 数据准备 / 清理
    # ------------------------------------------------------------------
    def _seed(self) -> None:
        """幂等 seed + 政策导入(与 scripts/demo_w2.py 的 ensure_data 同口径)。"""
        from scripts.load_policies import parse_policies, upsert_policies
        from scripts.seed import seed as seed_data

        with self.Session() as session:
            seed_data(session)
            upsert_policies(session, parse_policies(Path("data/policies")))
            session.commit()

    def reset(self) -> None:
        """清理运行时表(保留 seed 订单/政策/商户/工单基线)。"""
        with self.Session() as session:
            session.execute(
                delete(TicketMessage).where(
                    TicketMessage.ticket_id.notlike(SEED_TICKET_PREFIX)
                )
            )
            session.execute(
                delete(Ticket).where(Ticket.id.notlike(SEED_TICKET_PREFIX))
            )
            session.execute(delete(ConfirmRequest))
            session.execute(delete(ToolEvent))
            session.execute(delete(Memory))
            session.execute(delete(ChatMessage))
            session.execute(delete(ChatSession))
            session.execute(delete(RefundRequest))
            # TOCTOU 用例会临时篡改订单实付;每个 attempt 结束必须还原 seed 快照,
            # 否则下一次 attempt 会带着被改小的金额起跑(把"拒绝"误跑成"通过")。
            for seed_order in getattr(self, "_seed_orders", []):
                row = session.get(Order, seed_order["id"])
                if row is not None:
                    row.status = seed_order["status"]
                    row.total_cents = seed_order["total_cents"]
                    row.customer_id = seed_order["customer_id"]
            session.commit()

    def close(self) -> None:
        self.engine.dispose()

    def __enter__(self) -> "EvalEngine":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 快照与采集
    # ------------------------------------------------------------------
    @staticmethod
    def _snapshot_orders(session: Session) -> list[dict[str, Any]]:
        rows = session.scalars(select(Order).order_by(Order.id)).all()
        return [
            {
                "id": r.id,
                "status": r.status,
                "total_cents": int(r.total_cents),
                "customer_id": r.customer_id,
            }
            for r in rows
        ]

    @staticmethod
    def _write_counts(session: Session) -> dict[str, int]:
        refunds = session.scalar(select(func.count()).select_from(RefundRequest)) or 0
        tickets = session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.id.notlike(SEED_TICKET_PREFIX))
        ) or 0
        return {"refunds": int(refunds), "tickets": int(tickets)}

    def _customer_for(self, case: dict[str, Any], session: Session) -> str:
        context = dict(case.get("context") or {})
        explicit = context.get("current_customer_id")
        if explicit:
            return str(explicit)
        order_id = context.get("order_id")
        if order_id:
            row = session.get(Order, str(order_id))
            if row is not None:
                return str(row.customer_id)
        return DEFAULT_CUSTOMER_ID

    def _append_message(
        self,
        session: Session,
        *,
        session_id: str,
        role: str,
        content: str,
        trace_id: str,
        intent: str | None = None,
        attachments: list[str] | None = None,
    ) -> None:
        session.add(
            ChatMessage(
                session_id=session_id,
                role=role,
                content=content,
                attachments=list(attachments or []),
                trace_id=trace_id,
                intent=intent,
            )
        )
        session.flush()

    def _checkpoint_path(self, case_id: str, attempt: int) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in case_id)
        return self.checkpoint_dir / f"{safe}_{attempt}_{uuid.uuid4().hex[:8]}.db"

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def run_case(self, case: dict[str, Any], attempt: int = 1) -> dict[str, Any]:
        """跑一条用例一次,返回 artifact(证据包)。"""
        case_id = str(case.get("case_id") or "UNKNOWN")
        gt = dict(case.get("ground_truth") or {})
        context = dict(case.get("context") or {})
        raw_text = str(case.get("user_input") or "")
        attachments = list(context.get("attachments") or case.get("attachments") or [])
        repeat_turns = int(gt.get("repeat_turns") or 1)

        self.reset()
        artifact: dict[str, Any] = {
            "case_id": case_id,
            "attempt": attempt,
            "layer": str(case.get("layer") or ""),
            "input": {"raw": raw_text, "attachments": attachments, "context": context},
            "input_error": None,
            "truncated": False,
            "original_length": len(raw_text),
            "session_id": None,
            "replies": [],
            "reply": "",
            "final_reply": "",
            "session_status": None,
            "refunds": [],
            "tickets": [],
            "memory": [],
            "confirm": {"asked": False},
            "trace": [],
            "tool_names": [],
            "failed_tools": [],
            "orders": {"before": [], "after": [], "intact": True},
            "flags": {
                "replay_writes": 0,
                "idempotency_writes": 0,
                "toctou_error_code": None,
                "followup_ai_replied": None,
                # W5:blocked 轮 AI 真静默(零 AI 文本),这两个 flag 记录"空回复 + 系统回执"
                "followup_reply_empty": None,
                "followup_ack": None,
            },
            "latency_ms_total": 0,
            "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "estimated": True},
        }

        with self.Session() as session:
            artifact["orders"]["before"] = self._snapshot_orders(session)

        # ---- 1. 感知层:空输入 / 超长 / 非法附件在这里被拒/截断 ----
        try:
            percept = normalize(
                raw_text,
                attachments=attachments,
                locale=context.get("locale"),
                channel=context.get("channel"),
            )
        except PerceptionError as exc:
            artifact["input_error"] = {
                "code": "VALIDATION_ERROR",
                "message": str(exc),
                "field": exc.field,
            }
            with self.Session() as session:
                artifact["orders"]["after"] = self._snapshot_orders(session)
                artifact["orders"]["intact"] = (
                    artifact["orders"]["before"] == artifact["orders"]["after"]
                )
            return artifact

        artifact["truncated"] = bool(percept.truncated)
        artifact["original_length"] = (
            int(percept.original_length) if percept.original_length else len(raw_text)
        )

        usage_items: list[dict[str, Any]] = []
        trace_ids: list[str] = []
        checkpoint_path = self._checkpoint_path(case_id, attempt)
        last_result = None
        pending_seen: dict[str, Any] | None = None

        with self.Session() as session:
            customer_id = self._customer_for(case, session)
            session_row = ChatSession(
                merchant_id=self.merchant_id,
                customer_id=customer_id,
                status="active",
            )
            session.add(session_row)
            session.flush()
            artifact["session_id"] = session_row.id

            # ---- 2. 主对话轮(repeat_turns 轮:同一问题连发,验证升级/转人工) ----
            for turn in range(repeat_turns):
                trace_id = new_trace_id()
                trace_ids.append(trace_id)
                self._append_message(
                    session,
                    session_id=session_row.id,
                    role="user",
                    content=percept.text,
                    trace_id=trace_id,
                    attachments=list(percept.attachments),
                )
                started = time.monotonic()
                if session_row.status != "active":
                    result = runner.build_blocked_turn(
                        session=session,
                        session_row=session_row,
                        trace_id=trace_id,
                        text=percept.text,
                    )
                else:
                    result = runner.handle_turn(
                        session=session,
                        session_row=session_row,
                        percept_text=percept.text,
                        context=context,
                        trace_id=trace_id,
                        llm=self.llm,
                        checkpoint_path=checkpoint_path,
                    )
                latency_ms = int((time.monotonic() - started) * 1000)
                session.refresh(session_row)
                # W5 真静默:blocked 轮 reply 为空且 AI 没说话,不再补空 assistant 消息
                if result.ai_replied:
                    self._append_message(
                        session,
                        session_id=session_row.id,
                        role="assistant",
                        content=result.reply,
                        trace_id=trace_id,
                        intent=result.intent,
                    )
                session.commit()
                usage_items.append(_usage(percept.text, result.reply))
                artifact["replies"].append(
                    {
                        "stage": f"turn{turn + 1}",
                        "text": result.reply,
                        "intent": result.intent,
                        "ai_replied": bool(result.ai_replied),
                        "session_status": result.session_status,
                        "pending_confirm": bool(result.pending_confirm),
                        "latency_ms": latency_ms,
                    }
                )
                last_result = result
                if result.pending_confirm:
                    # 保留"最近一次出现过的确认卡":转人工后后续轮被路由守卫拦截,
                    # last_result 的 pending_confirm 会变空,但图仍停在确认门
                    pending_seen = dict(result.pending_confirm)

            # ---- 3. 确认流:按 ground_truth 决定是否批准确认卡 ----
            pending = dict(
                (last_result.pending_confirm if last_result else None)
                or pending_seen
                or {}
            )
            # W11 B8:确认卡挂着但未确认(工单未建)= "待确认转人工"进行态;
            # 供 hard_assert 在 handed_off 断言上按新语义兼容(不改 eval 数据)。
            artifact["handoff_pending"] = (
                str(pending.get("tool") or "") == "handoff__create_ticket"
            )
            token = str(pending.get("confirm_token") or "")
            if token:
                confirm_row = session.scalar(
                    select(ConfirmRequest).where(ConfirmRequest.confirm_token == token)
                )
                artifact["confirm"] = self._confirm_view(confirm_row)
                should_approve = bool(
                    gt.get("approve") is True
                    or gt.get("refund_created") is True
                    or gt.get("ticket_created") is True
                    # TOCTOU 回归:即使期望最终拒绝,也必须把 approve 送进确认门
                    or gt.get("toctou_tamper_total_cents") is not None
                )
                if should_approve and confirm_row is not None:
                    # TOCTOU:确认卡已按旧金额生成,批准前篡改订单实付
                    tamper = gt.get("toctou_tamper_total_cents")
                    if tamper is not None:
                        order_id = str(gt.get("toctou_order_id") or context.get("order_id") or "")
                        order_row = session.get(Order, order_id) if order_id else None
                        if order_row is not None:
                            order_row.total_cents = int(tamper)
                            session.commit()
                    started = time.monotonic()
                    trace_id = new_trace_id()
                    trace_ids.append(trace_id)
                    result, error_code = self._safe_resume(
                        session,
                        session_row=session_row,
                        checkpoint_path=checkpoint_path,
                    )
                    latency_ms = int((time.monotonic() - started) * 1000)
                    session.refresh(session_row)
                    session.commit()
                    usage_items.append(_usage("确认", result.reply if result else ""))
                    artifact["replies"].append(
                        {
                            "stage": "approve",
                            "text": result.reply if result else "",
                            "intent": result.intent if result else "",
                            "ai_replied": bool(result.ai_replied) if result else False,
                            "session_status": result.session_status if result else None,
                            "pending_confirm": False,
                            "latency_ms": latency_ms,
                            "error_code": error_code,
                        }
                    )
                    approve_code = error_code or _error_code_in_reply(
                        result.reply if result else ""
                    )
                    if gt.get("toctou_tamper_total_cents") is not None:
                        artifact["flags"]["toctou_error_code"] = approve_code
                    # 确认卡状态可能已变为 confirmed(重新查一次)
                    session.refresh(confirm_row)
                    artifact["confirm"] = self._confirm_view(confirm_row)

                    # 恢复重放:重复 resume 不应产生第二次写
                    if gt.get("replay_resume"):
                        before = self._write_counts(session)
                        _res, replay_err = self._safe_resume(
                            session,
                            session_row=session_row,
                            checkpoint_path=checkpoint_path,
                        )
                        session.commit()
                        after = self._write_counts(session)
                        artifact["flags"]["replay_writes"] = sum(
                            max(0, after[k] - before[k]) for k in before
                        )
                        artifact["replies"].append(
                            {
                                "stage": "replay_resume",
                                "text": _res.reply if _res else "",
                                "intent": _res.intent if _res else "",
                                "ai_replied": bool(_res.ai_replied) if _res else False,
                                "session_status": _res.session_status if _res else None,
                                "pending_confirm": False,
                                "latency_ms": 0,
                                "error_code": replay_err,
                            }
                        )

                    # 幂等重试:同一问题再发一次,不应重复建单
                    if gt.get("idempotency_retry"):
                        before = self._write_counts(session)
                        _r, _code = self._send_turn(
                            session,
                            session_row=session_row,
                            text=percept.text,
                            context=context,
                            checkpoint_path=checkpoint_path,
                            trace_ids=trace_ids,
                            stage="idempotency_retry",
                        )
                        session.commit()
                        after = self._write_counts(session)
                        artifact["flags"]["idempotency_writes"] = sum(
                            max(0, after[k] - before[k]) for k in before
                        )
                        usage_items.append(_usage(percept.text, _r.reply if _r else ""))
                        last_result = _r or last_result

            # ---- 4. 转人工后探针:AI 是否真的停止自动回复 ----
            if gt.get("followup_input") or gt.get("ai_stops_reply") is True:
                followup = str(gt.get("followup_input") or DEFAULT_FOLLOWUP)
                before_counts = self._write_counts(session)
                _r, _code = self._send_turn(
                    session,
                    session_row=session_row,
                    text=followup,
                    context=context,
                    checkpoint_path=checkpoint_path,
                    trace_ids=trace_ids,
                    stage="followup",
                )
                session.commit()
                after_counts = self._write_counts(session)
                artifact["flags"]["followup_ai_replied"] = (
                    bool(_r.ai_replied) if _r is not None else None
                )
                # W5:blocked 轮的"零 AI 文本 + 有系统回执"证据(不参与硬断言,供诊断)
                artifact["flags"]["followup_reply_empty"] = (
                    not str(_r.reply or "").strip() if _r is not None else None
                )
                artifact["flags"]["followup_ack"] = (
                    str(getattr(_r, "ack", "") or "") if _r is not None else None
                )
                artifact["flags"]["followup_writes"] = sum(
                    max(0, after_counts[k] - before_counts[k]) for k in before_counts
                )
                usage_items.append(_usage(followup, _r.reply if _r else ""))

            # ---- 5. 证据采集 ----
            session.refresh(session_row)
            artifact["session_status"] = session_row.status
            artifact["final_reply"] = (
                artifact["replies"][-1]["text"] if artifact["replies"] else ""
            )
            # 硬断言的数字/短语口径:全部 stage 回复按序拼接(见 __init__ artifact 契约)
            artifact["reply"] = "\n".join(
                str(item.get("text") or "")
                for item in artifact["replies"]
                if str(item.get("text") or "")
            )

            events = session.scalars(
                select(ToolEvent)
                .where(ToolEvent.trace_id.in_(trace_ids))
                .order_by(ToolEvent.step, ToolEvent.ts)
            ).all()
            trace_rows: list[dict[str, Any]] = []
            for ev in events:
                payload = _jsonable(ev.payload)
                trace_rows.append(
                    {
                        "step": int(ev.step or 0),
                        "tool_name": ev.tool_name,
                        "ok": bool(ev.ok),
                        "error_code": ev.error_code,
                        "payload": payload,
                    }
                )
            artifact["trace"] = trace_rows
            artifact["tool_names"] = [
                r["tool_name"] for r in trace_rows if r["tool_name"] in TOOL_NAMES
            ]
            artifact["failed_tools"] = [
                {
                    "tool_name": r["tool_name"],
                    "error_code": r["error_code"],
                    "payload": r["payload"],
                }
                for r in trace_rows
                if r["tool_name"] in TOOL_NAMES and not r["ok"]
            ]

            artifact["refunds"] = [
                {
                    "order_id": r.order_id,
                    "amount_cents": int(r.amount_cents),
                    "status": r.status,
                    "reason": r.reason,
                    "idempotency_key": r.idempotency_key,
                }
                for r in session.scalars(select(RefundRequest).order_by(RefundRequest.created_at)).all()
            ]
            artifact["tickets"] = [
                {
                    "id": t.id,
                    "status": t.status,
                    "handoff_reason": t.handoff_reason,
                    "order_id": t.order_id,
                    "customer_id": t.customer_id,
                }
                for t in session.scalars(
                    select(Ticket)
                    .where(Ticket.id.notlike(SEED_TICKET_PREFIX))
                    .order_by(Ticket.created_at)
                ).all()
            ]
            artifact["memory"] = [
                {
                    "id": m.id,
                    "text": m.text,
                    "status": m.status,
                    "executable": bool(m.executable),
                    "scope": m.scope,
                    "type": m.type,
                    "source": m.source,
                }
                for m in session.scalars(select(Memory).order_by(Memory.created_at)).all()
            ]
            artifact["orders"]["after"] = self._snapshot_orders(session)
            artifact["orders"]["intact"] = (
                artifact["orders"]["before"] == artifact["orders"]["after"]
            )

        artifact["usage"] = _sum_usage(usage_items)
        artifact["latency_ms_total"] = sum(
            int(r.get("latency_ms") or 0) for r in artifact["replies"]
        )
        return artifact

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _confirm_view(row: ConfirmRequest | None) -> dict[str, Any]:
        if row is None:
            return {"asked": False}
        payload = _jsonable(row.payload) or {}
        args = payload.get("args") if isinstance(payload, dict) else None
        if not isinstance(args, dict):
            args = payload if isinstance(payload, dict) else {}
        return {
            "asked": True,
            "tool_name": row.tool_name,
            "amount_cents": args.get("amount_cents"),
            "order_id": args.get("order_id"),
            "what": row.what,
            "impact": row.impact,
            "irreversibility": row.irreversibility,
            "alternatives": row.alternatives,
            "status": row.status,
            "confirm_token": row.confirm_token,
        }

    def _safe_resume(
        self,
        session: Session,
        *,
        session_row: ChatSession,
        checkpoint_path: Path,
    ) -> tuple[Any, str | None]:
        """把 approve 送回确认门;失败时返回 (None, error_code) 而不是中断评测。"""
        try:
            result = runner.resume(
                session_row.id,
                "approve",
                session=session,
                session_row=session_row,
                llm=self.llm,
                checkpoint_path=checkpoint_path,
            )
            return result, None
        except Exception as exc:  # noqa: BLE001 - 评测需要记录失败而不是崩掉整轮
            session.rollback()
            return None, str(getattr(exc, "code", "") or type(exc).__name__)

    def _send_turn(
        self,
        session: Session,
        *,
        session_row: ChatSession,
        text: str,
        context: dict[str, Any],
        checkpoint_path: Path,
        trace_ids: list[str],
        stage: str,
    ) -> tuple[Any, str | None]:
        """追加一轮用户消息(用于幂等重试 / 转人工后探针),不影响主断言口径。"""
        trace_id = new_trace_id()
        trace_ids.append(trace_id)
        self._append_message(
            session,
            session_id=session_row.id,
            role="user",
            content=text,
            trace_id=trace_id,
        )
        try:
            session.refresh(session_row)
            if session_row.status != "active":
                result = runner.build_blocked_turn(
                    session=session,
                    session_row=session_row,
                    trace_id=trace_id,
                    text=text,
                )
            else:
                result = runner.handle_turn(
                    session=session,
                    session_row=session_row,
                    percept_text=text,
                    context=context,
                    trace_id=trace_id,
                    llm=self.llm,
                    checkpoint_path=checkpoint_path,
                )
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return None, str(getattr(exc, "code", "") or type(exc).__name__)
        session.refresh(session_row)
        # W5 真静默:blocked 轮不补空 assistant 消息(见主循环同款处理)
        if result.ai_replied:
            self._append_message(
                session,
                session_id=session_row.id,
                role="assistant",
                content=result.reply,
                trace_id=trace_id,
                intent=result.intent,
            )
        session.commit()
        return result, None


def run_case(
    case: dict[str, Any],
    *,
    db_path: str | Path,
    checkpoint_dir: str | Path,
    attempt: int = 1,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """便捷函数:一次性建库→seed→跑一条→清理(单测/脚本均可用)。"""
    with EvalEngine(db_path=db_path, checkpoint_dir=checkpoint_dir, llm=llm) as eng:
        return eng.run_case(case, attempt=attempt)
