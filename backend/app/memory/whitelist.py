"""记忆白名单:什么类型 × 什么来源可写、默认 TTL、executable 判定。

三条硬规则(任务书 W3-B + PRD §5):

1. **政策/订单事实不进记忆**:检索归知识库、事实归 DB;``order_derived`` 只允许
   写 ``fact`` 类型(且调用方不得把订单字段整段抄进来);
2. **定价/折扣/承诺不进可执行记忆**:命中 ``PRICING_HINTS`` 的文本一律
   ``executable=False + status='quarantined'``;
3. **注入/越权指令不进可执行记忆**(W4-C-FIX / FIX-3):命中确定性注入特征的
   文本一律按定价指令待遇隔离(``executable=False``),不得生成可执行的
   偏好/事实;正当诉求由调用方先 ``strip_injection`` 再抽取;
4. **TTL 默认值**:偏好 180 天、事实 90 天、片段 30 天、流程 365 天。
"""
from __future__ import annotations

import re

from app.guardrails import detect_injection

#: memory.type 的四个合法值(与 0004 迁移的 CHECK 一致)
MEMORY_TYPES: tuple[str, ...] = ("fact", "episode", "preference", "procedure")

#: memory.source 的三个合法值(可审计来源)
MEMORY_SOURCES: tuple[str, ...] = ("user_stated", "order_derived", "agent_inferred")

#: 类型 × 来源白名单:值为允许写的来源集合
WRITABLE: dict[str, frozenset[str]] = {
    # 买家偏好只能来自用户自述或智能体归纳;不能把订单字段当偏好
    "preference": frozenset({"user_stated", "agent_inferred"}),
    # 事实可来自用户自述、订单派生、智能体归纳(仍受"订单事实归 DB"约束)
    "fact": frozenset({"user_stated", "order_derived", "agent_inferred"}),
    # 任务片段只允许智能体归纳
    "episode": frozenset({"agent_inferred"}),
    # 流程/习惯偏好来自用户自述或智能体归纳
    "procedure": frozenset({"user_stated", "agent_inferred"}),
}

#: 默认 TTL(天):None 表示不过期
DEFAULT_TTL_DAYS: dict[str, int | None] = {
    "preference": 180,
    "fact": 90,
    "episode": 30,
    "procedure": 365,
}

#: 低于该 decay_score 的记忆不再召回(任务书:衰减后低于阈值不召回)
MIN_DECAY_SCORE: float = 0.3

#: scope 的人类可读标签(注入文案用);未知 scope 原样输出
SCOPE_LABELS: dict[str, str] = {
    "size_recommendation": "尺码推荐",
    "pricing": "价格与折扣",
    "after_sale": "售后推荐",
    "logistics": "物流偏好",
}

#: 默认 scope(抽取器判断不出更具体范围时)
DEFAULT_SCOPE = "after_sale"

#: 注入指令的隔离 scope(同 pricing:只落 quarantined 备注行,永不注入)
INJECTION_SCOPE = "injection"

#: 定价/折扣/承诺指令的判定词(命中即隔离,不作为可执行记忆)
PRICING_HINTS: tuple[str, ...] = (
    "折扣",
    "打折",
    "改价",
    "降价",
    "优惠",
    "便宜",
    "返现",
    "免单",
    "赠品",
    "包邮",
    "承诺",
    "保证",
    "最低价",
)

#: "8 折 / 8.5折 / 八折 / 打 8 折" 等折扣表达
_DISCOUNT_RE = re.compile(
    r"(?:打\s*)?(?:[0-9]+(?:\.[0-9]+)?|[一二三四五六七八九])\s*折"
)


def is_pricing_instruction(text: str) -> bool:
    """文本是否包含定价/折扣/承诺指令(命中即必须隔离)。"""
    if not text:
        return False
    if _DISCOUNT_RE.search(text):
        return True
    return any(hint in text for hint in PRICING_HINTS)


def is_injection_instruction(text: str) -> bool:
    """文本是否命中确定性注入特征(SQL/命令/伪造工具调用/越权指令等)。

    复用 ``app.guardrails.detect_injection`` 的单一特征清单,保证与感知层
    (``Percept.flags``)和图上硬拦截使用同一套规则,不产生口径漂移。
    """
    if not text:
        return False
    return bool(detect_injection(text)["suspect"])


def can_write(memory_type: str, source: str, text: str | None = None) -> bool:
    """类型 × 来源是否在白名单内。

    传入 ``text`` 时再叠加一道隔离维度:命中注入的文本**不得作为可执行记忆**
    写入,返回 False(调用方应改为写 ``status='quarantined'`` 的隔离备注行,
    见 ``store._injection_candidate`` / ``store.create_memory``);不传 ``text``
    时行为与旧版完全一致,不影响既有调用方。
    """
    allowed = WRITABLE.get(memory_type)
    if not (allowed and source in allowed):
        return False
    if text is not None and is_injection_instruction(text):
        return False
    return True


def default_ttl_days(memory_type: str) -> int | None:
    """该类型的默认 TTL(天);未知类型返回 None(不自动过期,由调用方负责)。"""
    return DEFAULT_TTL_DAYS.get(memory_type)


def scope_label(scope: str | None) -> str:
    """scope 的人类可读标签,用于注入文案的"仅用于…"声明。"""
    if not scope:
        return SCOPE_LABELS[DEFAULT_SCOPE]
    return SCOPE_LABELS.get(scope, scope)
