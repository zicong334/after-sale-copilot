"""handoff__create_ticket 工具:创建人工转接工单(write,requires_confirm=True)。

任务书要求工单带 ``handoff_reason + summary``;但 W1-A 冻结的 ``Ticket`` 模型只有
``handoff_reason`` 一个 Text 列、**没有 summary 列**(模型缺口,照 tickets.py close
的先例汇报、不自行改模型)。故 summary 折进 ``handoff_reason``(换行分隔),两个信息
都保留;execute 返回时分别回传 reason/summary。

- ``handler``(invoke 走这里):resolve(若带 order_id,须存在且属于 ctx.customer_id)+
  返回确认卡四要素;不落业务数据。
- ``execute``(W3 invoke_confirmed / 测试走这里):真实创建 Ticket(queued)。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import Order, Ticket
from app.tools.registry import ToolSpec, clean_schema, register
from app.tools.results import (
    CODE_NOT_FOUND_OR_FORBIDDEN,
    ToolContext,
    ToolExecutionError,
)

logger = logging.getLogger("app.tools.handoff")

TOOL_NAME = "handoff__create_ticket"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    order_id: str | None = Field(default=None, pattern=r"^ORD-[0-9A-Z-]+$")


INPUT_SCHEMA: dict[str, Any] = clean_schema(Input)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "string"},
        "status": {"type": "string"},
        "reason": {"type": "string"},
        "summary": {"type": "string"},
        "order_id": {"type": ["string", "null"]},
    },
    "required": ["ticket_id", "status", "reason", "summary", "order_id"],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name=TOOL_NAME,
    description=(
        "创建人工客服工单,把当前会话转交给人工坐席跟进。"
        "用于问题复杂、超出自动化工具能处理的范围(如投诉、纠纷、需要人工核实)时;"
        "不要用它记录普通咨询(应先用 policy__search 检索政策解答)。"
    ),
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    side_effect="write",
    timeout_s=10.0,
    retry={"max_attempts": 2, "backoff_s": 0.5},
    idempotency_key=None,
    requires_confirm=True,
)


def _resolve_order(args: dict[str, Any], ctx: ToolContext) -> None:
    """若带 order_id,校验订单存在且属于当前客户(否则 NOT_FOUND_OR_FORBIDDEN)。"""
    order_id = args.get("order_id")
    if order_id is None:
        return
    order = ctx.session.get(Order, order_id)
    if order is None or order.customer_id != ctx.customer_id:
        logger.info(
            "handoff__create_ticket 真因=订单不存在/越权: order_id=%s", order_id,
        )
        raise ToolExecutionError(
            CODE_NOT_FOUND_OR_FORBIDDEN,
            "当前会话下未找到该订单",
            retryable=False,
            param="order_id",
        )


def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """invoke 路径:resolve 校验 + 生成确认卡四要素,不落业务数据。"""
    _resolve_order(args, ctx)
    reason = args["reason"]
    summary = args["summary"]
    order_id = args.get("order_id")
    return {
        "card": {
            "what": f"创建人工客服工单(转接原因:{reason})",
            "impact": "生成排队工单(queued),由人工坐席领取后跟进;会话转交人工",
            "irreversibility": "工单一旦创建即进入人工队列,无法自动撤回",
            "alternatives": "若问题可用现有政策工具解答,可先尝试 policy__search 自助解决",
        },
        "args": {
            "reason": reason,
            "summary": summary,
            "order_id": order_id,
        },
    }


def execute(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """真实创建工单(W3 invoke_confirmed / 测试用)。summary 折进 handoff_reason。"""
    _resolve_order(args, ctx)
    reason = args["reason"]
    summary = args["summary"]
    order_id = args.get("order_id")

    ticket = Ticket(
        merchant_id=ctx.merchant_id,
        customer_id=ctx.customer_id,
        order_id=order_id,
        status="queued",
        handoff_reason=f"{reason}\n摘要:{summary}",
    )
    ctx.session.add(ticket)
    ctx.session.commit()
    ctx.session.refresh(ticket)
    return {
        "ticket_id": ticket.id,
        "status": ticket.status,
        "reason": reason,
        "summary": summary,
        "order_id": order_id,
    }


register(SPEC, Input, handler)
