"""政策工具(两个):policy__search(检索条款)+ policy__check_return(可退性判定)。

- ``policy__search``:用 W9 混合检索(BM25 + 稠密向量 → RRF)在商户知识库上
  检索 active 条款,返回条款号 + 版本 + 原文 + 融合分;低于融合分阈值显式
  ``no_hit=True``(政策未覆盖,禁止硬凑)。
- ``policy__check_return``:纯函数(不查库),按订单状态 / 退款原因 / 签收天数判定
  ``eligible`` 并给出条款号;规则数字从 data/policies/*.md 提取,不手写映射。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.guardrails import order_status_label
from app.retrieval.hybrid import get_retriever
from app.tools.registry import ToolSpec, clean_schema, register
from app.tools.results import ToolContext

# ---------------------------------------------------------------------------
# policy__search
# ---------------------------------------------------------------------------
SEARCH_TOOL_NAME = "policy__search"


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


SEARCH_INPUT_SCHEMA: dict[str, Any] = clean_schema(SearchInput)

SEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause_id": {"type": "string"},
                    "version": {"type": "string"},
                    "text": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["clause_id", "version", "text", "score"],
                "additionalProperties": False,
            },
        },
        "no_hit": {"type": "boolean"},
    },
    "required": ["results", "no_hit"],
    "additionalProperties": False,
}

SEARCH_SPEC = ToolSpec(
    name=SEARCH_TOOL_NAME,
    description=(
        "检索店铺退换货/物流政策条款,返回条款号、版本与原文。"
        "用于回答'能不能退/运费谁出/多久到账'等政策问题;"
        "不要用它判断某笔具体订单能否退(应先用 shop__get_order 取状态,再用 "
        "policy__check_return 判定)。"
    ),
    input_schema=SEARCH_INPUT_SCHEMA,
    output_schema=SEARCH_OUTPUT_SCHEMA,
    side_effect="read",
    timeout_s=5.0,
    retry={"max_attempts": 1, "backoff_s": 0.0},
    idempotency_key=None,
    requires_confirm=False,
)


def search_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """混合检索(BM25 + 向量 RRF)商户知识库;只取 active 条款。

    W9 变更(最小 diff):检索实现从 ``BM25Backend`` 换成 ``HybridRetriever``。
    索引按"数据签名"进程内缓存,条款增删改后自动重建;``POST /api/kb/reload``
    可显式强制重建。工具名/入参/返回结构保持不变。
    """
    retriever = get_retriever(ctx.session, ctx.merchant_id)
    result = retriever.search(args["query"], top_k=args["top_k"])
    return {
        "results": [
            {
                "clause_id": c.clause_id,
                "version": c.version,
                "text": c.text,
                "score": c.score,
            }
            for c in result.results
        ],
        "no_hit": result.no_hit,
    }


# ---------------------------------------------------------------------------
# policy__check_return
# ---------------------------------------------------------------------------
CHECK_TOOL_NAME = "policy__check_return"

# 可退原因 → (最长期限/天, 对应条款号)。数字来自 data/policies/*.md:
# RP-01 七天无理由 7 天;RP-03 质量问题/错发漏发 15 天;LP-04 物流破损 24h(≈1 天)。
RETURN_RULES: dict[str, dict[str, Any]] = {
    "七天无理由": {"max_days": 7, "clause_id": "RP-01"},
    "质量问题": {"max_days": 15, "clause_id": "RP-03"},
    "错发漏发": {"max_days": 15, "clause_id": "RP-03"},
    "物流破损": {"max_days": 1, "clause_id": "LP-04"},
}

# 退货退款只对"已签收(delivered)"订单适用(政策天数均从签收起算)。
REFUNDABLE_STATUSES = {"delivered"}

# 超期统一引用条款(RP-12 拒收与超期退回)。
OVERDUE_CLAUSE_ID = "RP-12"


class CheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_status: str = Field(min_length=1)
    reason: Literal["质量问题", "错发漏发", "七天无理由", "物流破损"]
    days_since_delivery: int = Field(ge=0)


CHECK_INPUT_SCHEMA: dict[str, Any] = clean_schema(CheckInput)

CHECK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "eligible": {"type": "boolean"},
        "clause_id": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["eligible", "clause_id", "reason"],
    "additionalProperties": False,
}

CHECK_SPEC = ToolSpec(
    name=CHECK_TOOL_NAME,
    description=(
        "判定一笔已签收订单按给定原因是否可退/可换,返回 eligible、条款号与判定理由。"
        "仅做规则判定、不产生任何操作;"
        "不要用它替代退款下单(判定 eligible 且用户确认后,用 refund__create 建申请单)。"
    ),
    input_schema=CHECK_INPUT_SCHEMA,
    output_schema=CHECK_OUTPUT_SCHEMA,
    side_effect="read",
    timeout_s=5.0,
    retry={"max_attempts": 1, "backoff_s": 0.0},
    idempotency_key=None,
    requires_confirm=False,
)


#: 未发货/不可退状态 → 对客中文引导(不出现英文枚举,不硬邦邦驳回)。
NON_REFUNDABLE_GUIDANCE: dict[str, str] = {
    "created": (
        "订单当前为待付款状态,尚未发货,无需申请退货退款;"
        "如果不想买了,可直接在订单页取消订单,已支付金额将原路退回。"
        "如已完成付款或对订单有疑问,请核对订单号后告知我。"
    ),
    "paid": (
        "订单当前为待发货状态,尚未出库;如需取消,可在订单页取消并申请退款。"
        "如已发货,请提供订单号,我帮您核对物流进度。"
    ),
    "shipped": (
        "订单当前为运输中状态,请等待签收后再申请退货退款;"
        "若商品在途破损,请按物流破损流程处理并核对订单号。"
    ),
    "closed": "订单已关闭,无需退货退款;如对订单有疑问,请核对订单号后告知我。",
    "refunded": (
        "该订单此前已退款,不支持重复申请退款;若退款未到账,请核对订单号后告知我,我帮您查询进度。"
    ),
}


def _check_return(order_status: str, reason: str, days_since_delivery: int) -> dict[str, Any]:
    """可退性判定(纯函数,规则来自 data/policies/*.md)。

    W13-ISS-05:对客文案一律中文,不出现 created/delivered 等英文枚举;
    未发货订单给"取消订单/核对订单号"的可执行引导,而不是生硬驳回。
    """
    if order_status not in REFUNDABLE_STATUSES:
        guidance = NON_REFUNDABLE_GUIDANCE.get(str(order_status or "").strip().lower())
        if guidance is None:
            label = order_status_label(order_status) or "未知"
            guidance = (
                f"订单当前状态为{label},暂不支持退货退款;"
                "请核对订单号是否正确,如需帮助可申请人工客服。"
            )
        return {"eligible": False, "clause_id": "", "reason": guidance}
    rule = RETURN_RULES.get(reason)
    if rule is None:
        return {"eligible": False, "clause_id": "", "reason": f"不支持的原因类型:{reason}"}
    if days_since_delivery > rule["max_days"]:
        return {
            "eligible": False,
            "clause_id": OVERDUE_CLAUSE_ID,
            "reason": f"已超 {rule['max_days']} 天期限,原则上不予受理",
        }
    return {
        "eligible": True,
        "clause_id": rule["clause_id"],
        "reason": f"符合{reason}政策,可按流程为您办理",
    }


def check_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """可退性判定(纯函数,不查库)。"""
    return _check_return(args["order_status"], args["reason"], args["days_since_delivery"])


register(SEARCH_SPEC, SearchInput, search_handler)
register(CHECK_SPEC, CheckInput, check_handler)
