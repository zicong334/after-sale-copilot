"""LLM 抽象层(W2-C 新增;W4-C 接入真实模型)。

对外暴露:
- ``LLMClient`` / ``FakeLLM`` / 两个 schema;
- ``build_llm()``:按**当前进程环境变量**(``LLM_API_KEY``)自动选真实客户端或
  ``FakeLLM``。**本函数不读 .env**:加载 .env 是脚本/启动器显式行为,避免测试在
  有 .env 的机器上误触真实 API(烧钱且破坏 Fake 密封)。
- ``RealLLMClient`` / 异常类 / ``UsageStats``(W4-C 真实调用)。
"""

from __future__ import annotations

import os

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
from app.llm.router import ModelRouter

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


def _build_fallback_client() -> RealLLMClient | None:
    """按环境变量构造热备客户端;未配置备胎时返回 None。

    W13:主用 DeepSeek 之外的备胎(如 Minimax)通过 ``LLM_FALLBACK_*`` 注入;
    三项都为空 = 不启用路由,保持"单客户端"的旧行为(测试 D-05 密封不变)。
    """
    api_key = (os.environ.get("LLM_FALLBACK_API_KEY") or "").strip()
    base_url = (os.environ.get("LLM_FALLBACK_BASE_URL") or "").strip()
    if not api_key and not base_url:
        return None
    client = RealLLMClient(
        api_key=api_key or None,
        base_url=base_url or None,
        model=(os.environ.get("LLM_FALLBACK_MODEL") or "MiniMax-Text-01").strip(),
    )
    # 档位/供应商标记让 trace、回执、健康检查能区分主备。
    client.tier = "fallback"
    client.provider = "minimax"
    return client


def build_llm(**kwargs) -> LLMClient:
    """按环境变量选择 LLM 实现:有 ``LLM_API_KEY`` → RealLLMClient,否则 FakeLLM。

    只读取 ``os.environ``,不读 .env;真实模式由调用方显式注入(见
    ``backend/scripts/run_eval.py --real``、S6 启动脚本)。

    W13 接线:配置了备胎(``LLM_FALLBACK_API_KEY`` / ``LLM_FALLBACK_BASE_URL``)
    时返回 ``ModelRouter(primary, fallback)``——主用熔断后自动切备胎,两档都挂
    再用规则引擎兜底;没配备胎仍返回裸 ``RealLLMClient``(既有契约/测试不变)。
    """
    if not has_real_key():
        return FakeLLM()
    primary = RealLLMClient(**kwargs)
    fallback = _build_fallback_client()
    if fallback is None:
        return primary
    return ModelRouter(primary, fallback)
