"""api/chat.py — 会话薄路由(W2-C 新增;W3-C 增 SSE 流式壳)。

路由只做"无业务"的四件事:
1. 请求校验 + 感知归一化(``app.perception``),非法输入 400;
2. 取/建 ``chat_session``,并做**会话状态守卫**:非 active 直接固定话术、不调编排
   (G7 判定点:转人工后 AI 停止自动回复,防止人工已接手还在自动答);
3. 落库 user 消息(trace_id / attachments) → 调编排 → 落库 assistant 消息(trace_id / intent);
4. 原样返回编排结果(intent / citations / pending_confirm / tool_calls / trace_id)。

W3-C 新增 ``POST /api/c/chat/stream``:手写 ``text/event-stream``(FastAPI
StreamingResponse,不引入 sse-starlette),事件类型:

- ``message``:回复分片(本棒无真实 LLM,按字符切"模拟流式",注释见 ``_chunks``);
- ``citation``:一条政策引用(``[n]`` 与前端高亮对应);
- ``confirm_request``:待确认卡(pending_confirm 原样:what/impact/irreversibility/
  alternatives/confirm_token);
- ``handoff_notice``:会话已转人工(handed_off),前端显示横幅;
- ``append_ack``:W5 真静默轮的系统回执(买家消息已追加进工单,不是 AI 气泡);
- ``usage``:本轮计量 ``{tokens_in, tokens_out, cost_usd, latency_ms, estimated:true}``
  (FakeLLM 口径:按字符数估算,MOCK 价目,非真实账单);
- ``done``:本轮结束,带 session_id / trace_id / 完整结果摘要。

W5 真静默(修正一):会话已 handed_off 的轮次**不产生任何 AI 文本**——不落
assistant ``chat_message``、响应体整键不含 ``usage``;SSE 只发 ``append_ack`` +
``done``;``handoff_notice`` 只在"转人工创建当轮"(仍有 AI 回复的那一轮)发一次。

所有 DB 工作与图执行都在 ``_run_turn`` 内同步完成,生成器只 yield 已算好的帧,
避免在流式响应里持有请求级 Session。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.errors import AppError, NotFoundError
from app.models import ChatMessage, ChatSession, Ticket
from app.graph import runner
from app.llm import LLMError, build_llm
from app.llm.base import stream_sink
from app.perception import PerceptionError, normalize
from app.pii import MaskingLLM, PIIMasker
from app.trace import new_trace_id, record_event

router = APIRouter(prefix="/api/c", tags=["chat"])

# MOCK 计量:本棒不接真实 LLM,按字符数估 token(中文约 2 字符/token),
# 价目仅用于前端展示口径,金额单位 USD、不是真实账单。
_CHARS_PER_TOKEN = 2
_PRICE_IN_PER_MTOK = 0.15
_PRICE_OUT_PER_MTOK = 0.60
_MESSAGE_CHUNK_CHARS = 12

#: W7-2 降级话术:确定性安全话术,不得声称任何写操作已完成(退款/提交/处理)。
DEGRADED_REPLY = "服务暂忙,已记录您的诉求,稍后可重试或转人工。"
#: W7-2 trace 事件名:沿用 tool_events 表 + 命名空间区分事件类别。
EVENT_LLM_DEGRADED = "llm_degraded"


class ChatRequest(BaseModel):
    """进线请求。

    ``context`` 是会话槽位(已由上游/上一轮解析出的确定性字段),不是感知层内容:
    ``order_id`` / ``days_since_delivery`` / ``unresolved_rounds`` /
    ``claimed_refund_cents`` / ``claimed_paid_cents`` / ``refund_amount_cents``。

    ⚠️ ``merchant_id`` / ``customer_id``:仅 AUTH_MODE=internal 回归兜底;
    enforce 下无身份效力(生效身份一律取自 token,见 ``security.effective_identity``)。
    """

    model_config = ConfigDict(extra="forbid")

    merchant_id: str | None = Field(default=None, min_length=1, max_length=64)
    customer_id: str | None = Field(default=None, min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)
    text: str
    attachments: list[str] | None = None
    locale: str | None = None
    channel: str | None = None
    context: dict[str, Any] | None = None


def _tokens(chars: int) -> int:
    return max(1, (max(0, chars) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def _usage(
    text_in: str, text_out: str, latency_ms: int, llm: Any | None = None
) -> dict[str, Any]:
    """本轮用量:FakeLLM 按字符估算;真实客户端优先用 API 返回的真实 usage。"""
    client_usage = getattr(llm, "usage", None)
    if client_usage is not None and int(getattr(client_usage, "requests", 0) or 0) > 0:
        return {
            "tokens_in": int(client_usage.prompt_tokens),
            "tokens_out": int(client_usage.completion_tokens),
            "reasoning_tokens": int(client_usage.reasoning_tokens),
            "cost_usd": client_usage.cost_usd(
                getattr(llm, "price_in_per_mtok", None),
                getattr(llm, "price_out_per_mtok", None),
            ),
            "latency_ms": latency_ms,
            "estimated": False,
            "estimation": "真实 API usage(单价未配置时 cost_usd=null/TBD)",
        }
    tokens_in = _tokens(len(text_in))
    tokens_out = _tokens(len(text_out))
    cost = (
        tokens_in * _PRICE_IN_PER_MTOK + tokens_out * _PRICE_OUT_PER_MTOK
    ) / 1_000_000
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 8),
        "latency_ms": latency_ms,
        "estimated": True,
        "estimation": f"MOCK: {_CHARS_PER_TOKEN} 字符/token,${_PRICE_IN_PER_MTOK}/M in,${_PRICE_OUT_PER_MTOK}/M out",
    }


def _degraded_result(
    *,
    db: Session,
    trace_id: str,
    session_status: str,
    error: LLMError,
    model_name: str,
) -> runner.TurnResult:
    """W7-2:LLM 故障降级结果(HTTP 200 + ``degraded:true``,该轮不落 assistant 消息)。

    审计事件 ``llm_degraded`` 只记录故障元信息(不写用户文本),供事后定位;
    降级话术是确定性常量,不声称任何写操作已完成。
    """
    status_code = getattr(error, "status_code", None)
    record_event(
        db,
        trace_id=trace_id,
        event=EVENT_LLM_DEGRADED,
        payload={
            "error_type": type(error).__name__,
            "status_code": status_code,
            "model": model_name,
            "message": str(error)[:200],
        },
        ok=False,
        error_code="LLM_DEGRADED",
    )
    return runner.TurnResult(
        intent="degraded",
        confidence=1.0,
        intent_reason="LLM 调用失败,本轮降级为确定性安全话术",
        classification_path="llm_degraded",
        reply=DEGRADED_REPLY,
        citations=[],
        pending_confirm=None,
        tool_calls=[],
        session_status=session_status,
        ai_replied=True,
    )


def _run_turn(
    db: Session, payload: ChatRequest, identity: security.Identity
) -> dict[str, Any]:
    """一轮对话:感知 → 落库 → 编排 → 落库;返回对外响应体(含 usage)。

    身份统一从 ``security.current_identity`` 注入,请求体里的 merchant/customer
    只在 internal 模式下兜底(``effective_identity`` 负责,enforce 下无效)。
    """
    ident = security.effective_identity(identity, "buyer", payload=payload)
    # 日配额在入口处检查;超限直接 429,本轮不产生任何写入。
    security.enforce_token_quota(db, ident.sub)
    # 生效身份覆盖请求体归属字段:后续建会话/归属校验一律用 ident 的值。
    payload = payload.model_copy(
        update={"merchant_id": ident.merchant_id, "customer_id": ident.customer_id}
    )
    try:
        percept = normalize(
            payload.text,
            attachments=payload.attachments,
            locale=payload.locale,
            channel=payload.channel,
        )
    except PerceptionError as exc:
        raise AppError(
            "VALIDATION_ERROR", str(exc), status_code=400, param=exc.field
        ) from exc

    # trace_id 归属本轮:ChatSession 表(W2-B 冻结,无 trace_id 列)不存该值,
    # 而是随本轮 user/assistant 两条 chat_message 与 tool_events 一起落库,
    # 验收要求的"会话带 trace_id"按"消息 + 事件同 trace_id 可反查"口径满足。
    trace_id = new_trace_id()
    # W7-1:每轮独立 PII 映射器;出网文本统一走占位符,库内存原文。
    masker = PIIMasker()
    masked_text = masker.mask(percept.text)
    session_row = _resolve_session(db, payload)
    # 提前捕获:降级回滚后 ORM 实例可能失效,响应仍需稳定的会话标识。
    session_id = session_row.id
    session_status = str(session_row.status or "active")

    user_msg = ChatMessage(
        session_id=session_row.id,
        role="user",
        content=percept.text,
        attachments=list(percept.attachments),
        trace_id=trace_id,
    )
    db.add(user_msg)
    db.flush()

    started = time.monotonic()
    llm = None
    degraded = False
    if session_status != "active":
        # 会话已被人工接手/关闭:记录一条拦截事件后停止自动回复。
        result = runner.build_blocked_turn(
            session=db, session_row=session_row, trace_id=trace_id, text=percept.text
        )
    else:
        # W4-C:按进程环境自动选真实/Fake(本函数不读 .env;S6 由启动器注入环境,
        # 测试环境无 LLM_API_KEY 时仍是 FakeLLM,D-05 密封不变)
        # W7-1:只在 chat 层包一层 MaskingLLM(不改 build_llm 默认返回值),
        # 出网消息 mask、回包 restore;编排层与工具看到的是占位符文本。
        llm = MaskingLLM(build_llm(), masker)
        # W7-2:SAVEPOINT 包裹本轮图执行,LLM 故障时可回滚本轮未提交写入;
        # 工具内部会自行 commit(此后 savepoint 失效),已提交的 user 消息保留。
        savepoint = db.begin_nested()
        try:
            result = runner.handle_turn(
                session=db,
                session_row=session_row,
                percept_text=masked_text,
                context=payload.context,
                trace_id=trace_id,
                llm=llm,
            )
            if savepoint.is_active:
                savepoint.commit()
        except LLMError as exc:
            if savepoint.is_active:
                savepoint.rollback()
            else:
                db.rollback()
            degraded = True
            result = _degraded_result(
                db=db,
                trace_id=trace_id,
                session_status=session_status,
                error=exc,
                model_name=str(getattr(llm, "model_name", "")),
            )
    latency_ms = int((time.monotonic() - started) * 1000)

    # W7-1:展示/落库前把占位符换回原文(回复可能已由 MaskingLLM 反解,幂等)。
    result.reply = masker.restore(result.reply)
    for citation in result.citations:
        snippet = citation.get("snippet")
        if isinstance(snippet, str):
            citation["snippet"] = masker.restore(snippet)
    if not degraded:
        user_msg.intent = result.intent
    # W5 真静默:blocked 轮 AI 零吐字(reply=""),不落空 assistant 消息;
    # 买家这条文本已由 runner.build_blocked_turn 追加进该会话的未关闭工单。
    # W7-2 降级轮同样不落 assistant 消息(本轮没有可展示的 AI 生成内容)。
    blocked = not result.ai_replied
    assistant_msg: ChatMessage | None = None
    if not blocked and not degraded:
        assistant_msg = ChatMessage(
            session_id=session_row.id,
            role="assistant",
            content=result.reply,
            attachments=[],
            trace_id=trace_id,
            intent=result.intent,
        )
        db.add(assistant_msg)
    # W8:本轮计量在 commit 前累加(record_token_usage 不自行 commit);
    # blocked 轮没有 AI 生成,不计费也不写用量。
    usage: dict[str, Any] | None = (
        None if blocked else _usage(percept.text, result.reply, latency_ms, llm=llm)
    )
    if usage is not None:
        security.record_token_usage(
            db, ident.sub, usage["tokens_in"], usage["tokens_out"]
        )
    db.commit()

    # W8:WS 事件一律在 commit 之后发布(订阅端只认已落库的事实)。
    # 本函数所有分支都拿得到 session_id,故无需跳过。
    if assistant_msg is not None:
        security.publish_event(
            security.channel_buyer(session_id),
            "assistant_message",
            session_id=session_id,
            message_id=assistant_msg.id,
            trace_id=trace_id,
        )

    body = result.to_dict()
    body.update(
        {
            "ok": True,
            "session_id": session_id,
            "trace_id": trace_id,
            "truncated": percept.truncated,
            "original_length": percept.original_length,
            # W7-1:占位符 → 原文映射,供前端把回复里的占位符换回原文;无 PII 时为 {}。
            "pii_restore": masker.restore_mapping(),
        }
    )
    if degraded:
        body["degraded"] = True
    # W8:转人工创建当轮(blocked 轮已把买家文本追加进工单)广播排队事件,
    # 坐席队列页据此即时刷新。载荷里的 order_id/customer_id 从工单行查出;
    # 无工单号或工单已不可见时不发。
    ack_ticket_id = body.get("ack_ticket_id")
    if blocked and ack_ticket_id:
        ticket_row = db.get(Ticket, ack_ticket_id)
        if ticket_row is not None:
            queue_payload: dict[str, Any] = {
                "ticket_id": ticket_row.id,
                "session_id": session_id,
                "customer_id": ticket_row.customer_id,
            }
            if ticket_row.order_id:
                queue_payload["order_id"] = ticket_row.order_id
            security.publish_event(
                security.channel_queue(ident.merchant_id), "queue_new", **queue_payload
            )
    if usage is not None:
        # 非 blocked 轮 usage 照旧;blocked 轮没有 AI 生成,整键不出现(不计费)
        body["usage"] = usage
    return body


@router.post("/chat")
def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> dict[str, Any]:
    """一轮对话(JSON):感知 → 落库 → 编排 → 落库 → 返回。"""
    return _run_turn(db, payload, identity)


def _chunks(text: str) -> Iterator[str]:
    """模拟流式分片:无真实 LLM(本棒禁止接 key),按固定字符窗口切分。"""
    for start in range(0, len(text), _MESSAGE_CHUNK_CHARS):
        yield text[start : start + _MESSAGE_CHUNK_CHARS]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _frames(body: dict[str, Any]) -> Iterator[str]:
    """把一轮结果展开成 SSE 帧(全部在同步阶段算好,生成器零 IO)。

    W5 真静默:blocked 轮只发 ``append_ack`` + ``done``——没有 AI 文本可播,
    也不发 ``usage`` / ``handoff_notice``(横幅只在转人工创建当轮发一次)。
    """
    if body.get("ai_replied") is False:
        yield _sse(
            "append_ack",
            {
                "ack": body.get("ack") or "",
                "ticket_id": body.get("ack_ticket_id"),
                "session_status": body.get("session_status") or "handed_off",
            },
        )
        yield _sse(
            "done",
            {
                "ok": True,
                "session_id": body.get("session_id"),
                "trace_id": body.get("trace_id"),
                "intent": body.get("intent"),
                "confidence": body.get("confidence"),
                "classification_path": body.get("classification_path"),
                "session_status": body.get("session_status"),
                "ai_replied": False,
                "tool_calls": body.get("tool_calls") or [],
                "reply": "",
                "ack": body.get("ack") or "",
                "ack_ticket_id": body.get("ack_ticket_id"),
            },
        )
        return

    reply = str(body.get("reply") or "")
    for delta in _chunks(reply):
        yield _sse("message", {"delta": delta})
    for citation in body.get("citations") or []:
        yield _sse("citation", dict(citation))
    if body.get("pending_confirm"):
        card = dict(body["pending_confirm"])
        card.setdefault("thread_id", body.get("session_id"))
        yield _sse("confirm_request", card)
    if body.get("session_status") == "handed_off" and body.get("ai_replied") is True:
        # 仅"转人工创建当轮"发一次;后续 blocked 轮在上面的早退分支不会走到这里
        yield _sse(
            "handoff_notice",
            {
                "session_status": "handed_off",
                "reply_stopped": False,
                "message": "已转人工,AI 已停止自动回复,坐席会尽快跟进。",
            },
        )
    yield _sse("usage", dict(body.get("usage") or {}))
    done: dict[str, Any] = {
        "ok": True,
        "session_id": body.get("session_id"),
        "trace_id": body.get("trace_id"),
        "intent": body.get("intent"),
        "confidence": body.get("confidence"),
        "classification_path": body.get("classification_path"),
        "session_status": body.get("session_status"),
        "ai_replied": body.get("ai_replied"),
        "tool_calls": body.get("tool_calls"),
        "reply": reply,
        "usage": body.get("usage"),
    }
    if body.get("degraded"):
        # W7-2:SSE 端同样透出降级标记,前端可显示"服务暂忙"提示。
        done["degraded"] = True
    yield _sse("done", done)


#: W10-4 真流式:工作线程与 SSE 生成器之间的哨兵(表示"整轮已算完")
_STREAM_DONE = object()


def _stream_frames(
    payload: ChatRequest, db: Session, identity: security.Identity
) -> Iterator[str]:
    """真流式(W10-4):工作线程跑整轮,SSE 生成器边收边吐 ``delta`` 增量。

    契约(与旧 ``_frames`` 兼容,只多一个 ``delta`` 事件类型):

    1. ``delta``:模型生成一段就吐一段(逐字增量),前端据此逐字渲染;
    2. 整轮结束后仍按旧契约补发 ``message``(12 字分片,拼接 == done.reply)/
       ``citation`` / ``confirm_request`` / ``handoff_notice`` / ``usage`` / ``done``;
       前端收到 ``done`` 后整卡重渲,保证最终正文与守卫后的定稿一致;
    3. 阻塞轮(blocked)没有 AI 正文:不产生 ``delta``,只发 ``append_ack`` + ``done``。

    整轮在**工作线程**里跑(会话级 Session 不跨线程共享:worker 自开一个
    Session 连同一个 engine),SSE 生成器只做"取增量 → yield"。
    """
    deltas: "queue.Queue[Any]" = queue.Queue()
    outcome: dict[str, Any] = {}

    def _push(piece: str) -> None:
        deltas.put(piece)

    def _worker() -> None:
        try:
            engine = db.get_bind()
            # 会话级 Session 不跨线程共享:worker 自开一个 Session 连同一个 engine
            with Session(engine) as worker_db:
                with stream_sink(_push):
                    outcome["body"] = _run_turn(worker_db, payload, identity)
        except BaseException as exc:  # noqa: BLE001 - 异常统一转成降级帧
            outcome["error"] = exc
        finally:
            deltas.put(_STREAM_DONE)

    thread = threading.Thread(target=_worker, name="chat-stream", daemon=True)
    thread.start()

    while True:
        item = deltas.get()
        if item is _STREAM_DONE:
            break
        yield _sse("delta", {"delta": str(item)})

    error = outcome.get("error")
    if error is not None:
        # 编排层异常(与同步路径同口径):固定降级话术 + done 帧,不裸断连接。
        yield _sse(
            "message", {"delta": DEGRADED_REPLY}
        )
        yield _sse("usage", {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                            "latency_ms": 0, "estimated": True})
        yield _sse(
            "done",
            {
                "ok": False,
                "session_id": None,
                "trace_id": None,
                "intent": "degraded",
                "confidence": 0.0,
                "classification_path": "llm_degraded",
                "session_status": "active",
                "ai_replied": True,
                "tool_calls": [],
                "reply": DEGRADED_REPLY,
                "usage": None,
                "degraded": True,
                "error": type(error).__name__,
            },
        )
        return

    yield from _frames(dict(outcome.get("body") or {}))


@router.post("/chat/stream")
def chat_stream(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    identity: security.Identity = Depends(security.current_identity),
) -> StreamingResponse:
    """一轮对话(SSE):同一套编排,按事件流返回(真流式:边生成边吐增量)。"""
    # 预校验放在请求线程:非法输入仍走同步路径的 400 语义,不必等流开始。
    try:
        normalize(
            payload.text,
            attachments=payload.attachments,
            locale=payload.locale,
            channel=payload.channel,
        )
    except PerceptionError as exc:
        raise AppError(
            "VALIDATION_ERROR", str(exc), status_code=400, param=exc.field
        ) from exc
    return StreamingResponse(
        _stream_frames(payload, db, identity),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _resolve_session(db: Session, payload: ChatRequest) -> ChatSession:
    """按 session_id 取会话,或为新客户开一个 active 会话(归属必须匹配)。"""
    if payload.session_id:
        session_row = db.get(ChatSession, payload.session_id)
        if (
            session_row is None
            or session_row.merchant_id != payload.merchant_id
            or session_row.customer_id != payload.customer_id
        ):
            raise NotFoundError("会话不存在或不属于该客户")
        return session_row
    session_row = ChatSession(
        merchant_id=payload.merchant_id,
        customer_id=payload.customer_id,
        status="active",
    )
    db.add(session_row)
    db.flush()
    return session_row
