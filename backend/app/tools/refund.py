"""refund__create 工具:创建退款申请单(write,requires_confirm=True)。

- ``handler``(invoke 走这里):resolve(订单存在 + 归属)+ 业务规则(状态可退 /
  金额≤实付 / 无活跃退款单),全过才返回确认卡四要素;不落业务数据。
- ``execute``(W3 invoke_confirmed / 测试走这里):真实调 W1-B
  ``create_refund_request``(幂等链),落 refund_request。

幂等键(PRD §5 + 任务书):``sha256(order_id|refund_request|reason|amount_cents)``。
同幂等键重复调用只产生一张退款单(execute 里先查同键活跃单,命中即幂等返回);
同订单不同幂等键(不同原因/金额)才是"重复创建"业务拒绝。

注:execute 主动做活跃单检查,以便同幂等键命中时走"幂等返回已有单"而非服务层
的竞态分支;服务层 ``StateConflictError(..., param=...)`` 的签名已由缺陷修复包
F2 补齐(见 ``app/errors.py``),两者语义不冲突。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.errors import AmountExceedsPaidError
from app.models import Order, RefundRequest
from app.services.refunds import create_refund_request
from app.tools.registry import ToolSpec, clean_schema, register
from app.tools.results import (
    CODE_BUSINESS_REJECT,
    CODE_NOT_FOUND_OR_FORBIDDEN,
    ToolContext,
    ToolExecutionError,
)

logger = logging.getLogger("app.tools.refund")

TOOL_NAME = "refund__create"

# 退货退款只对"已签收(delivered)"订单适用(与 policy__check_return 一致)。
REFUNDABLE_STATUSES = {"delivered"}

# 活跃退款单状态(uq_refund_active 部分唯一索引覆盖的范围)。
ACTIVE_REFUND_STATUSES = ("申请", "审核", "退款")

# 幂等键里 action 标签(PRD §5 的固定段)。
ACTION_LABEL = "refund_request"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{2,63}$")
    amount_cents: int = Field(gt=0)
    reason: Literal["质量问题", "错发漏发", "七天无理由", "物流破损"]


INPUT_SCHEMA: dict[str, Any] = clean_schema(Input)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "refund_id": {"type": "string"},
        "order_id": {"type": "string"},
        "amount_cents": {"type": "integer"},
        "reason": {"type": "string"},
        "status": {"type": "string"},
        "idempotency_key": {"type": "string"},
        "idempotent_hit": {"type": "boolean"},
    },
    "required": [
        "refund_id", "order_id", "amount_cents", "reason",
        "status", "idempotency_key", "idempotent_hit",
    ],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name=TOOL_NAME,
    description=(
        "创建退款申请单(不是直接退款)。仅在 policy__check_return 判定 eligible、"
        "且用户已确认后调用;不要用它查询退款进度。"
    ),
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    side_effect="write",
    timeout_s=10.0,
    retry={"max_attempts": 2, "backoff_s": 0.5},
    idempotency_key=["order_id", "reason", "amount_cents"],
    requires_confirm=True,
)


def compute_refund_key(order_id: str, reason: str, amount_cents: int) -> str:
    """退款幂等键:sha256(order_id|refund_request|reason|amount_cents)。"""
    raw = f"{order_id}|{ACTION_LABEL}|{reason}|{amount_cents}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve(args: dict[str, Any], ctx: ToolContext) -> Order:
    """resolve + 基础业务规则:订单存在 + 归属 + 状态可退 + 金额≤实付。

    不存在/越权统一 NOT_FOUND_OR_FORBIDDEN(对外不区分真因);状态不可退/金额超
    实付为 BUSINESS_REJECT。返回订单。
    """
    order_id = args["order_id"]
    amount_cents = args["amount_cents"]

    order = ctx.session.get(Order, order_id)
    if order is None or order.customer_id != ctx.customer_id:
        logger.info(
            "refund__create 真因=订单不存在/越权: order_id=%s", order_id,
        )
        raise ToolExecutionError(
            CODE_NOT_FOUND_OR_FORBIDDEN,
            "当前会话下未找到该订单",
            retryable=False,
            param="order_id",
        )
    if order.status not in REFUNDABLE_STATUSES:
        raise ToolExecutionError(
            CODE_BUSINESS_REJECT,
            f"订单状态 {order.status} 不支持退款",
            retryable=False,
            param="status",
        )
    if amount_cents > order.total_cents:
        raise ToolExecutionError(
            CODE_BUSINESS_REJECT,
            f"退款金额 {amount_cents} 分超过订单实付 {order.total_cents} 分",
            retryable=False,
            param="amount_cents",
        )
    return order


def _active_refund(ctx: ToolContext, order_id: str) -> RefundRequest | None:
    """同订单当前活跃退款单(申请/审核/退款),无则 None。"""
    return ctx.session.execute(
        select(RefundRequest).where(
            RefundRequest.order_id == order_id,
            RefundRequest.status.in_(ACTIVE_REFUND_STATUSES),
        )
    ).scalars().first()


def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """invoke 路径:resolve + 业务规则 + 确认卡四要素,不落业务数据。"""
    order = _resolve(args, ctx)
    key = compute_refund_key(args["order_id"], args["reason"], args["amount_cents"])
    # 业务规则:任何活跃退款单都不再生成确认卡(幂等重试/不同键重复都在此拦截)
    if _active_refund(ctx, order.id) is not None:
        raise ToolExecutionError(
            CODE_BUSINESS_REJECT,
            "该订单已存在活跃退款单,不能重复创建",
            retryable=False,
            param="order_id",
        )
    return {
        "card": {
            "what": f"为订单 {order.id} 创建一笔 {args['amount_cents']} 分的退款申请单(原因:{args['reason']})",
            "impact": "生成退款申请单并进入人工审核流程,审核通过后退款原路到账",
            "irreversibility": "申请单一旦生成不可直接撤销,需人工审核或驳回",
            "alternatives": "可先联系客服确认原因,或选择其他退款原因后再提交",
        },
        "args": {
            "order_id": order.id,
            "amount_cents": args["amount_cents"],
            "reason": args["reason"],
            "idempotency_key": key,
        },
    }


def _format(refund: RefundRequest, hit: bool) -> dict[str, Any]:
    return {
        "refund_id": refund.id,
        "order_id": refund.order_id,
        "amount_cents": refund.amount_cents,
        "reason": refund.reason,
        "status": refund.status,
        "idempotency_key": refund.idempotency_key,
        "idempotent_hit": hit,
    }


def execute(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """真实创建退款申请单(W3 invoke_confirmed / 测试幂等用)。幂等:同键只一张单。"""
    order = _resolve(args, ctx)
    key = compute_refund_key(order.id, args["reason"], args["amount_cents"])

    active = _active_refund(ctx, order.id)
    if active is not None:
        # 同幂等键命中 → 幂等返回已有单;不同键 → 重复创建业务拒绝
        if active.idempotency_key == key:
            return _format(active, hit=True)
        raise ToolExecutionError(
            CODE_BUSINESS_REJECT,
            "该订单已存在活跃退款单,不能重复创建",
            retryable=False,
            param="order_id",
        )

    try:
        refund, hit = create_refund_request(
            ctx.session,
            ctx.merchant_id,
            order.id,
            args["amount_cents"],
            args["reason"],
            key,
        )
    except AmountExceedsPaidError as exc:
        raise ToolExecutionError(
            CODE_BUSINESS_REJECT,
            str(exc),
            retryable=False,
            param="amount_cents",
        )
    return _format(refund, hit)


register(SPEC, Input, handler)
