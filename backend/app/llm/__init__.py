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
    INTENT_WEATHER,
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
from app.llm.router import TIER_FALLBACK, TIER_PRIMARY, CircuitBreaker, ModelRouter

#: 进程级共享熔断器(F4)。
#:
#: 为什么必须共享:``api/chat.py`` 每轮对话都调一次 ``build_llm()``。若每次都新建
#: 熔断器,"连续 3 次失败开路 / 冷却 30s / 半开探针"这套状态机在真实 HTTP 路径上
#: 跨请求永远不生效——每轮都从零开始,主用已知挂了也仍要先撞一次、付满超时才降级。
#:
#: 为什么只共享熔断器、不共享整个 router:``ModelRouter.attach_trace()`` 每轮改写
#: 实例上的 ``_session``/``_trace_id``;共享 router 会让并发请求(异步端点 + 作业
#: worker 线程)串错追踪归属。客户端仍按每轮新建,行为不变。
#:
#: 为什么在模块导入期就建好(而不是懒加载):模块导入受解释器导入锁保护,
#: 天然免除"两个线程同时懒初始化"的竞态,比加锁懒加载更简单。
#: 对象本身无 IO、无网络, FakeLLM 密封路径下也只是两个空壳。
_PRIMARY_BREAKER = CircuitBreaker(name=TIER_PRIMARY)
_FALLBACK_BREAKER = CircuitBreaker(name=TIER_FALLBACK)

__all__ = [
    "LLMClient",
    "FakeLLM",
    "RealLLMClient",
    "build_llm",
    "reset_shared_breakers",
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
    "INTENT_WEATHER",
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


def reset_shared_breakers() -> None:
    """把进程级共享熔断器换回初始闭合态(**测试隔离用**,生产路径不调)。

    与 ``security.reset_security_state()`` / ``semantic_cache.clear()`` 同一模式:
    进程级单例必须逐用例重置,否则前一个用例把主用熔断成 open,后一个用例
    拿到的就是脏状态。conftest 的 autouse fixture 前后各调一次。
    """
    global _PRIMARY_BREAKER, _FALLBACK_BREAKER
    _PRIMARY_BREAKER = CircuitBreaker(name=TIER_PRIMARY)
    _FALLBACK_BREAKER = CircuitBreaker(name=TIER_FALLBACK)


def build_llm(**kwargs) -> LLMClient:
    """按环境变量选择 LLM 实现:有 ``LLM_API_KEY`` → RealLLMClient,否则 FakeLLM。

    只读取 ``os.environ``,不读 .env;真实模式由调用方显式注入(见
    ``backend/scripts/run_eval.py --real``、S6 启动脚本)。

    W13 接线:配置了备胎(``LLM_FALLBACK_API_KEY`` / ``LLM_FALLBACK_BASE_URL``)
    时返回 ``ModelRouter(primary, fallback)``——主用熔断后自动切备胎,两档都挂
    再用规则引擎兜底;没配备胎仍返回裸 ``RealLLMClient``(既有契约/测试不变)。

    F4:两个熔断器取**进程级共享**实例,使熔断状态跨请求存活(见模块顶部说明)。
    """
    if not has_real_key():
        return FakeLLM()
    primary = RealLLMClient(**kwargs)
    fallback = _build_fallback_client()
    if fallback is None:
        return primary
    return ModelRouter(
        primary,
        fallback,
        breaker=_PRIMARY_BREAKER,
        fallback_breaker=_FALLBACK_BREAKER,
    )
