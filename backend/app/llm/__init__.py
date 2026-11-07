"""LLM 抽象层(W2-C 新增;W4-C 接入真实模型)。

对外暴露:
- ``LLMClient`` / ``FakeLLM`` / 两个 schema;
- ``build_llm()``:按**当前进程环境变量**(``LLM_API_KEY``)自动选真实客户端或
  ``FakeLLM``。**本函数不读 .env**:加载 .env 是脚本/启动器显式行为,避免测试在
  有 .env 的机器上误触真实 API(烧钱且破坏 Fake 密封)。
- ``RealLLMClient`` / 异常类 / ``UsageStats``(W4-C 真实调用)。
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
from app.llm.real_client import (
    LLMApiError,
    LLMBatchAbort,
    LLMConfigError,
    LLMEmptyContentError,
    LLMError,
    RealLLMClient,
    UsageStats,
    has_real_key,
)

__all__ = [
    "LLMClient",
    "FakeLLM",
    "RealLLMClient",
    "build_llm",
    "has_real_key",
    "LLMApiError",
    "LLMBatchAbort",
    "LLMConfigError",
    "LLMEmptyContentError",
    "LLMError",
    "UsageStats",
    "Message",
    "SCHEMA_INTENT",
    "SCHEMA_REPLY",
    "build_facts_message",
    "INTENT_POLICY_QA",
    "INTENT_ORDER_JUDGE",
    "INTENT_HANDOFF",
    "INTENT_OTHER",
]


def build_llm(**kwargs) -> LLMClient:
    """按环境变量选择 LLM 实现:有 ``LLM_API_KEY`` → RealLLMClient,否则 FakeLLM。

    只读取 ``os.environ``,不读 .env;真实模式由调用方显式注入(见
    ``backend/scripts/run_eval.py --real``、S6 启动脚本)。
    """
    if not has_real_key():
        return FakeLLM()
    return RealLLMClient(**kwargs)
