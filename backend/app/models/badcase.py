"""badcase 模块:坏例回流表 ``badcase``(W12-2 数据飞轮)。

一行 = 坐席在工作台点「标记坏例」时的现场快照,打包:

- ``trace_id`` + 转录(``transcript``):转人工前 AI 问答 + 工单消息;
- 工具链(``tool_chain``):该 trace 下的 ``tool_events``(工具名/成败/错误码/参数);
- judge 分(``judge_score`` / ``judge_detail``):实时工单默认不跑离线裁判,
  由评测/人工裁决注入;缺省时 ``judge_detail`` 显式记录 ``source=unavailable``,
  不伪造分数;
- 坐席修改 diff(``suggestion`` 建议原文 / ``agent_final`` 坐席最终回复 / ``diff``)。

设计约束
--------
- **不带外键**:坏例是审计/回流素材,必须能在工单被删除后仍留存
  (与 ``turn_audit`` 同一取舍),因此只存标量归属字段;
- ``status`` 用 CHECK 挡非法值:``new`` → ``promoted``(已生成 eval 草稿)/
  ``ignored``(判定不是坏例);
- **不写 ``data/eval/cases_v1.json``**:草稿由 ``scripts/promote_badcase.py``
  落到 ``data/eval/drafts/``,入集走主会话的版本升级治理流程;
- 时间列一律 UTC(``utc_now``),主键 ``gen_id()`` uuid4 字符串(与全库一致)。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base


class BadCase(Base):
    """坏例回流行(工作台「标记坏例」→ eval 草稿的数据源)。"""

    __tablename__ = "badcase"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    # 审计归属:工单可能被删除,故不建外键(与 turn_audit 一致)。
    ticket_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    merchant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    handoff_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    service_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 坐席建议原文 / 坐席最终公开回复 / 两者 diff(unified,可能为空)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_final: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON 文本:转录 / 工具链 / judge 明细
    transcript: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    tool_chain: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    judge_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    judge_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", index=True,
    )
    # promote 成功后回填草稿 case_id(便于草稿 → 坏例反查)
    eval_case_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('new','promoted','ignored')",
            name="ck_badcase_status",
        ),
        CheckConstraint(
            "judge_score IS NULL OR (judge_score >= 0 AND judge_score <= 1)",
            name="ck_badcase_judge_score",
        ),
        Index("ix_badcase_status_created", "status", "created_at"),
    )
