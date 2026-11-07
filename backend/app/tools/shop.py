"""shop__get_order 工具:查询当前客户名下的一笔订单(read)。

resolve 校验(订单存在 + 属于 ctx.customer_id),失败统一返回
``NOT_FOUND_OR_FORBIDDEN``——对外不区分"不存在"与"越权",真因只记内部日志
(G4 卡 + 任务书 shop.py 要求)。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import Order
from app.tools.registry import ToolSpec, clean_schema, register
from app.tools.results import (
    CODE_NOT_FOUND_OR_FORBIDDEN,
    ToolContext,
    ToolExecutionError,
)

logger = logging.getLogger("app.tools.shop")

TOOL_NAME = "shop__get_order"


class Input(BaseModel):
    """shop__get_order 入参:order_id 必须匹配 ``^ORD-[0-9A-Z-]+$``。"""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(pattern=r"^ORD-[0-9A-Z-]+$")


INPUT_SCHEMA: dict[str, Any] = clean_schema(Input)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string"},
        "status": {"type": "string"},
        "total_cents": {"type": "integer"},
        "currency": {"type": "string"},
        "created_at": {"type": "string"},
    },
    "required": ["order_id", "status", "total_cents", "currency", "created_at"],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name=TOOL_NAME,
    description=(
        "查询当前客户名下的一笔订单详情(订单号、状态、实付金额、币种、下单时间)。"
        "仅用于读取订单信息,不做任何修改;不要用它查询退款进度或发起退款。"
    ),
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    side_effect="read",
    timeout_s=5.0,
    retry={"max_attempts": 1, "backoff_s": 0.0},
    idempotency_key=None,
    requires_confirm=False,
)


def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """查询订单;不存在或非本客户订单 → NOT_FOUND_OR_FORBIDDEN(不区分真因)。"""
    order_id = args["order_id"]
    order = ctx.session.get(Order, order_id)
    if order is None:
        logger.info("shop__get_order 真因=订单不存在: order_id=%s", order_id)
        raise ToolExecutionError(
            CODE_NOT_FOUND_OR_FORBIDDEN,
            "当前会话下未找到该订单",
            retryable=False,
            param="order_id",
        )
    if order.customer_id != ctx.customer_id:
        logger.info(
            "shop__get_order 真因=越权: order_id=%s 属于 %s,当前 %s",
            order_id, order.customer_id, ctx.customer_id,
        )
        raise ToolExecutionError(
            CODE_NOT_FOUND_OR_FORBIDDEN,
            "当前会话下未找到该订单",
            retryable=False,
            param="order_id",
        )
    return {
        "order_id": order.id,
        "status": order.status,
        "total_cents": order.total_cents,
        "currency": order.currency,
        "created_at": order.created_at.isoformat(),
    }


register(SPEC, Input, handler)
