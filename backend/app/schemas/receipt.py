"""通用回执 Receipt(PRD §4 回执标准 + 任务书 W1-B 硬要求)。

每个写操作响应体带:业务单号(order_id/ticket_id/refund_request_id 三选一)+
status(写完后重新 SELECT 回来的值,即 verified)+ occurred_at_utc + idempotent_hit。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Receipt(BaseModel):
    """写操作回执。业务单号按写操作类型三选一填充。"""

    order_id: str | None = None
    ticket_id: str | None = None
    refund_request_id: str | None = None
    status: str
    occurred_at_utc: datetime
    idempotent_hit: bool = False
