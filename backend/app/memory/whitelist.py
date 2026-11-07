"""记忆白名单:什么类型 × 什么来源可写、默认 TTL、executable 判定。

三条硬规则(任务书 W3-B + PRD §5):

1. **政策/订单事实不进记忆**:检索归知识库、事实归 DB;``order_derived`` 只允许
   写 ``fact`` 类型(且调用方不得把订单字段整段抄进来);
2. **定价/折扣/承诺不进可执行记忆**:命中 ``PRICING_HINTS`` 的文本一律
   ``executable=False + status='quarantined'``;
3. **TTL 默认值**:偏好 180 天、事实 90 天、片段 30 天、流程 365 天。
"""
from __future__ import annotations

import re

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


def can_write(memory_type: str, source: str) -> bool:
    """类型 × 来源是否在白名单内。"""
    allowed = WRITABLE.get(memory_type)
    return bool(allowed) and source in allowed


def default_ttl_days(memory_type: str) -> int | None:
    """该类型的默认 TTL(天);未知类型返回 None(不自动过期,由调用方负责)。"""
    return DEFAULT_TTL_DAYS.get(memory_type)


def scope_label(scope: str | None) -> str:
    """scope 的人类可读标签,用于注入文案的"仅用于…"声明。"""
    if not scope:
        return SCOPE_LABELS[DEFAULT_SCOPE]
    return SCOPE_LABELS.get(scope, scope)
