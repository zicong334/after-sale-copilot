"""LLM 抽象层(W2-C 新增)。

对外只暴露 ``LLMClient`` / ``FakeLLM`` / 两个 schema;当前实现全部是 MOCK。
"""

from app.llm.base import (
    INTENT_HANDOFF,
    INTENT_ORDER_JUDGE,
    INTENT_OTHER,
    INTENT_POLICY_QA,
    SCHEMA_INTENT,
    SCHEMA_REPLY,
    FakeLLM,
    LLMClient,
    Message,
    build_facts_message,
)

__all__ = [
    "LLMClient",
    "FakeLLM",
    "Message",
    "SCHEMA_INTENT",
    "SCHEMA_REPLY",
    "build_facts_message",
    "INTENT_POLICY_QA",
    "INTENT_ORDER_JUDGE",
    "INTENT_HANDOFF",
    "INTENT_OTHER",
]
