"""jobs 模块:作业表 ``job_queue`` 与审计落盘表 ``turn_audit``(W10-3)。

设计要点
--------
- ``job_queue`` 是 W10-3 异步任务队列的落地表:一行 = 一个待办作业。
  ``status`` 用 CHECK 约束把非法状态挡在库外,取值只有
  ``pending / running / done / dead``;``dead`` 即 dead-letter 行(重试耗尽后保留,
  不删除,便于事后排查)。
- ``attempts`` 记录**已尝试次数**(取作业时 +1);``max_attempts`` 默认 3,
  即"失败重试 2 次后进入 dead"的总尝试上限。
- ``turn_audit`` 是每轮对话的审计留痕:trace_id 可反查该轮的其它事件,
  ``citations`` 存 JSON 文本(引用列表),``reply`` 存本轮回复正文。
- 时间列一律 UTC(``utc_now``),主键一律 ``gen_id()`` uuid4 字符串(与全库一致)。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base


class Job(Base):
    """作业行(W10-3 作业队列)。

    ``payload`` 存 JSON 文本(作业参数);``kind`` 取值由 ``app.jobs`` 的常量定义
    (memory_extract / ticket_summary / turn_audit),但表层不做枚举约束——
    便于后续扩展作业类型而不改表。
    """

    __tablename__ = "job_queue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, onupdate=utc_now, server_default=func.current_timestamp(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','done','dead')",
            name="ck_job_queue_status",
        ),
        # 不变量:attempts / max_attempts 不得为负,max_attempts 至少 1
        CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1",
            name="ck_job_queue_attempts",
        ),
        Index("ix_job_queue_status_created", "status", "created_at"),
    )


class TurnAudit(Base):
    """每轮对话的审计落盘行(W10-3 ``turn_audit`` 作业的落地表)。

    不带外键:审计行必须在业务数据被删除后仍可留存(审计表的定位就是"事后可查"),
    因此只存标量归属字段与文本,``citations`` 存 JSON 文本。
    """

    __tablename__ = "turn_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    merchant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        Index("ix_turn_audit_trace_created", "trace_id", "created_at"),
    )
