"""LLM 抽象层 + FakeLLM(MOCK)实现。

⚠️ MOCK 声明(重要)
本文件中的 ``FakeLLM`` 是纯规则 mock:意图分类走常量关键词表,回复走模板拼装,
**不调用任何真实模型、不发网络请求、不需要 API key**。整条链路上凡是由它产出的
文本都应在回执/日志中视为 MOCK 输出。

W4-C 才接真实模型;届时只需新增一个满足 ``LLMClient`` 契约的真实实现(W4-C 在那里
注入 key 与模型),编排层 ``app.orchestrator.w1_router`` 无需改动——这正是本文件
把"理解"和"编排"分开的原因:W3 换图、W4 换模型,都不动业务调用顺序。

契约:
    client.complete(messages, schema=None) -> str
- ``messages``:OpenAI 风格 ``[{"role": "system"|"user"|"assistant", "content": str}]``;
- ``schema``:``None`` 表示要纯文本;否则返回**符合该 JSON Schema 的 JSON 字符串**,
  由调用方 ``json.loads`` 解析(与真实模型的结构化输出对齐);
- 结构化调用时,编排层把确定性事实以 ``{"facts": {...}}`` 的 system 消息附在末尾,
  FakeLLM 直接读这些事实做规则决策(W4-C 的真实模型读同一份上下文,不依赖该技巧)。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

# 消息类型:OpenAI 风格
Message = dict[str, str]

#: 流式分块长度(与 SSE 通道的 ``_MESSAGE_CHUNK_CHARS`` 对齐)
STREAM_CHUNK_CHARS = 12


class LLMClient(ABC):
    """LLM 客户端抽象:唯一方法 complete,便于 W4-C 平替真实模型。"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """模型标识(写入 trace / 回执,便于区分 MOCK 与真实模型)。"""

    @abstractmethod
    def complete(self, messages: list[Message], schema: dict[str, Any] | None = None) -> str:
        """返回模型输出;传入 schema 时返回 JSON 字符串。"""

    def stream_text(
        self,
        messages: list[Message],
        schema: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """真流式(W10):逐段吐出**给买家看的正文**(schema 的 ``reply`` 字段值)。

        默认实现是"非流式降级":一次性 complete() 后整段吐出,保证旧客户端
        (以及任何只实现 complete 的实现)都能直接用。真流式实现见
        ``RealLLMClient.stream_text``(OpenAI 兼容 SSE)与
        ``FakeLLM.stream_text``(分块模拟)。
        """
        raw = self.complete(messages, schema)
        text = reply_text_from_raw(raw, schema)
        if text:
            yield text


# ---------------------------------------------------------------------------
# 流式 sink(W10):compose 节点把增量推给上层(SSE delta 事件)
# ---------------------------------------------------------------------------

#: 当前线程的流式 sink(接收正文增量);None 表示本轮不流式。
_STREAM_SINK: ContextVar[Callable[[str], None] | None] = ContextVar(
    "llm_stream_sink", default=None
)


def current_stream_sink() -> Callable[[str], None] | None:
    """取当前线程的流式 sink(没有则 None)。"""
    return _STREAM_SINK.get()


@contextmanager
def stream_sink(sink: Callable[[str], None] | None) -> Iterator[None]:
    """在当前上下文绑定流式 sink(退出时还原)。"""
    token = _STREAM_SINK.set(sink)
    try:
        yield
    finally:
        _STREAM_SINK.reset(token)


def reply_text_from_raw(raw: str, schema: dict[str, Any] | None = None) -> str:
    """从模型原始输出里取出"给买家看的正文"。

    - 带 ``reply_text`` schema:解析 JSON 取 ``reply`` 字段(失败则回落为原串);
    - 无 schema:原样返回。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if schema and str(schema.get("name") or "") == SCHEMA_REPLY["name"]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            return str(data.get("reply") or "")
        return text
    return text


# ---------------------------------------------------------------------------
# 结构化输出的 schema(与真实模型 structured output 对齐)
# ---------------------------------------------------------------------------

SCHEMA_INTENT: dict[str, Any] = {
    "name": "intent_decision",
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["policy_qa", "order_judge", "handoff", "other"],
        },
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["intent", "confidence", "reason"],
    "additionalProperties": False,
}

SCHEMA_REPLY: dict[str, Any] = {
    "name": "reply_text",
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# FakeLLM(MOCK):规则分类 + 模板回复
# ---------------------------------------------------------------------------

#: 意图:政策问答
INTENT_POLICY_QA = "policy_qa"
#: 意图:订单判责(查单 + 查政策 + 必要时出退款确认卡)
INTENT_ORDER_JUDGE = "order_judge"
#: 意图:转人工(建工单 + 会话 handed_off + AI 停止回复)
INTENT_HANDOFF = "handoff"
#: 意图:其它(能力说明)
INTENT_OTHER = "other"

#: 规则表常量:W4-C 换真实模型时整表删除,保留字段名便于对照
HANDOFF_HINTS: tuple[str, ...] = (
    "转人工", "人工客服", "真人客服", "找人工", "人工坐席", "转接人工",
    "三遍", "都没解决", "没人管", "到底能不能处理",
)
RISK_HINTS: tuple[str, ...] = (
    "投诉", "曝光", "差评", "起诉", "12315", "必须按", "赔偿", "举报",
)
ORDER_TOPIC_HINTS: tuple[str, ...] = (
    "退货", "退款", "换货", "能退", "退给我", "判责", "查一下订单", "订单",
    "签收", "收到货",
)
POLICY_TOPIC_HINTS: tuple[str, ...] = (
    "政策", "几天", "多久", "多少天", "期限", "运费", "尺码", "多高", "多重",
    "无理由", "质量问题", "到账", "包邮", "发货", "偏远", "改地址", "拒收",
)
#: 记忆/偏好场景线索:买家在陈述长期偏好或要求"以后都推荐",不是即时政策问答。
#: 必须放在 POLICY_TOPIC_HINTS 判定之前("尺码"同时在政策词表里),但仍排在
#: 明示人工/风险信号/订单判责之后,保证既有分类顺序不被改变。
MEMORY_SCENE_HINTS: tuple[str, ...] = (
    "尺码", "偏好", "以后都给我", "以后都推荐", "以后推荐", "以后都选", "记住我",
)

#: 回复模板里的"其它"能力说明
_OTHER_REPLY = (
    "我可以帮您:① 查询退换货政策(期限/运费/尺码/到账时间);"
    "② 核对某笔订单能否退换(请提供订单号);③ 转接人工客服。"
    "请补充订单号或具体问题。"
)
#: 未命中政策条款时的说明(不臆造政策)
_NO_HIT_REPLY = (
    "抱歉,店铺政策里暂未找到与该问题直接相关的条款(检索无命中)。"
    "为避免误导,我不做推测;您可以补充具体商品场景,或转人工客服确认。"
)


class FakeLLM(LLMClient):
    """MOCK LLM:关键词规则分类 + 模板回复(无网络、无模型)。

    规则刻意放在模块级常量里、置信度只有"高置信快捷路径/规则命中"两档,
    避免出现"硬编码低置信度却无人消费"的反模式(参见 c_router 反例)。
    """

    @property
    def model_name(self) -> str:
        return "fake-rule-v1 (MOCK)"

    def complete(self, messages: list[Message], schema: dict[str, Any] | None = None) -> str:
        facts = _extract_facts(messages)
        name = (schema or {}).get("name")
        if name == SCHEMA_INTENT["name"]:
            text = str(facts.get("text") or _last_user_content(messages))
            decision = self.classify(text, facts)
            return json.dumps(decision, ensure_ascii=False)
        if name == SCHEMA_REPLY["name"]:
            return json.dumps({"reply": self.compose(facts)}, ensure_ascii=False)
        return json.dumps({"reply": "", "note": "FakeLLM 未支持的 schema"}, ensure_ascii=False)

    def stream_text(
        self,
        messages: list[Message],
        schema: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """分块模拟真流式(W10):正文与 ``complete()`` 的 reply 字段逐字一致。"""
        text = reply_text_from_raw(self.complete(messages, schema), schema)
        for start in range(0, len(text), STREAM_CHUNK_CHARS):
            yield text[start : start + STREAM_CHUNK_CHARS]

    # -- 意图分类 ---------------------------------------------------------
    def classify(self, text: str, facts: dict[str, Any] | None = None) -> dict[str, Any]:
        """规则表分类;返回 {intent, confidence, reason}。"""
        facts = facts or {}
        lowered = text.lower()
        has_order = bool(facts.get("has_order"))
        unresolved = int(facts.get("unresolved_rounds") or 0)

        if _hit(lowered, HANDOFF_HINTS):
            reason = "明示人工" if _hit(lowered, ("转人工", "人工客服", "真人客服", "找人工", "人工坐席")) else "连续多轮未解决"
            return {"intent": INTENT_HANDOFF, "confidence": 0.95, "reason": reason}
        if _hit(lowered, RISK_HINTS):
            return {"intent": INTENT_HANDOFF, "confidence": 0.95, "reason": "风险信号(投诉/金额争议)"}
        if unresolved >= 3:
            return {"intent": INTENT_HANDOFF, "confidence": 0.95, "reason": "连续 3 轮未解决"}
        if has_order and _hit(lowered, ORDER_TOPIC_HINTS):
            return {"intent": INTENT_ORDER_JUDGE, "confidence": 0.9, "reason": "命中订单话题且会话有订单"}
        if _hit(lowered, MEMORY_SCENE_HINTS):
            return {"intent": INTENT_OTHER, "confidence": 0.9, "reason": "记忆/偏好场景(由记忆注入处理)"}
        if _hit(lowered, POLICY_TOPIC_HINTS):
            return {"intent": INTENT_POLICY_QA, "confidence": 0.9, "reason": "命中政策话题"}
        if _hit(lowered, ORDER_TOPIC_HINTS):
            return {"intent": INTENT_ORDER_JUDGE, "confidence": 0.85, "reason": "命中订单话题(缺订单号,先追问)"}
        return {"intent": INTENT_OTHER, "confidence": 0.5, "reason": "未命中规则"}

    # -- 回复组装 ---------------------------------------------------------
    def compose(self, facts: dict[str, Any] | None = None) -> str:
        """按 facts["kind"] 选模板;所有数字/条款都来自 facts,不在此臆造。"""
        facts = facts or {}
        kind = facts.get("kind")

        if kind == "policy_answer":
            if facts.get("no_hit"):
                return _NO_HIT_REPLY
            clauses = facts.get("clauses") or []
            lines = [
                f"[{i}] {c['clause_id']}({c.get('version', '')}){c.get('title', '')}:{c['text']}"
                for i, c in enumerate(clauses, start=1)
            ]
            return (
                "根据店铺政策:\n" + "\n".join(lines)
                + "\n\n如需按上述政策办理,请提供订单号,我帮您核对是否在期限内。"
            )

        if kind == "order_verdict_eligible":
            clause = facts.get("clause") or {}
            citation = (
                f"[1] {clause.get('clause_id', '')}({clause.get('version', '')}):{_sentence(clause.get('text', ''))}"
                if clause
                else ""
            )
            amount = int(facts.get("amount_cents") or 0)
            return (
                f"已核对订单 {facts.get('order_id')}(状态:{facts.get('status')},"
                f"实付 ¥{amount / 100:.2f})。判定:{facts.get('verdict_reason')}。"
                f"依据条款 {citation}。"
                f"已生成退款申请确认卡:退款金额 ¥{amount / 100:.2f}({amount} 分),"
                f"原因「{facts.get('reason_text')}」。需要您确认后才会真正提交,请回复确认。"
            ).strip()

        if kind == "order_verdict_refused":
            clause = facts.get("clause") or {}
            citation = (
                f"[1] {clause.get('clause_id', '')}({clause.get('version', '')}):{_sentence(clause.get('text', ''))}"
                if clause
                else ""
            )
            return (
                f"已核对订单 {facts.get('order_id')}(状态:{facts.get('status')})。"
                f"判定:{facts.get('verdict_reason')}。依据条款 {citation}。"
                "该判定下无法直接受理退款;若您对判定有异议,可以申请人工复核。"
            ).strip()

        if kind == "order_not_found":
            return (
                "未能在当前会话下找到您提到的订单。请确认订单号是否正确;"
                "如需进一步核实,可以转人工客服。"
            )

        if kind == "order_missing_id":
            return (
                "可以帮您核对退换货资格,但当前会话里还没有可用的订单号,"
                "请提供订单号(例如 seed-order-019)。"
            )

        if kind == "handoff":
            order_part = f"(关联订单 {facts['order_id']})" if facts.get("order_id") else ""
            return (
                f"已为您转接人工客服{order_part}。转接原因:{facts.get('reason')}。"
                "工单已进入人工队列,坐席会尽快跟进;在人工接手前,我先不再自动回复。"
            )

        # other 分支:任务开始注入了历史偏好时,回复要引用记忆内容(如尺码偏好),
        # 没有记忆(count=0 / text 为空)则保持原能力菜单模板,避免空口承诺。
        memory = facts.get("memory")
        if isinstance(memory, dict) and int(memory.get("count") or 0) > 0:
            memory_text = str(memory.get("text") or "").strip()
            if memory_text:
                return _memory_reply(memory_text)

        return _OTHER_REPLY


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _hit(text: str, hints: tuple[str, ...]) -> bool:
    return any(h in text for h in hints)


def _sentence(text: str) -> str:
    """去掉条款原文末尾的句读,避免模板拼接后出现"。。"。"""
    return str(text).strip().rstrip("。;；. ")


def _memory_reply(memory_text: str) -> str:
    """other 分支引用记忆注入包:只取 "- [type/scope] 正文" 的正文,不把元数据抄给用户。"""
    points: list[str] = []
    for raw in memory_text.splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        detail = line[2:].strip()
        if detail.startswith("[") and "]" in detail:
            detail = detail.split("]", 1)[1].strip()
        if detail:
            points.append(detail)
    digest = "；".join(points) if points else _sentence(memory_text)
    return (
        f"已记下您的偏好。根据历史记忆:{digest}。"
        "后续我会参考这些偏好;涉及价格/退款/承诺仍以店铺政策和订单为准。"
        "如需修改或补充,请直接告诉我。"
    )


def _last_user_content(messages: list[Message]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def _extract_facts(messages: list[Message]) -> dict[str, Any]:
    """取编排层附带的确定性事实(最后一条可解析为含 facts 的 JSON 消息)。"""
    for msg in reversed(messages):
        raw = str(msg.get("content") or "").strip()
        if not raw.startswith("{"):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("facts"), dict):
            return data["facts"]
    return {}


def build_facts_message(facts: dict[str, Any]) -> Message:
    """构造携带确定性事实的 system 消息(FakeLLM / 真实模型共用上下文)。"""
    return {
        "role": "system",
        "content": json.dumps({"facts": facts}, ensure_ascii=False),
    }
