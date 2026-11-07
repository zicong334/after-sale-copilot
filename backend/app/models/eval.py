"""eval 模块:eval_case(评测用例,17 字段,见 W1-C)。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import gen_id, utc_now
from app.models import Base


class EvalCase(Base):
    """评测用例(新增,17 字段照 03§4.3.2 JSON 示例)。

    17 个业务字段 = case_id/layer/scene/user_input/context/ground_truth/
    expected_tools/forbidden_tools/expected_numbers/forbidden_phrases/
    must_ask_confirmation/expected_verdict/weight/repeat/rubric_version/source/notes。
    case_id UNIQUE 供按 case 幂等 upsert;id 为通用主键。

    不带 merchant_id 的理由:评测集是项目级资产(跨店铺统一验收口径),
    不归属单店;如需按店筛选评测,由后续任务书加列。
    """

    __tablename__ = "eval_case"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_id)
    case_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    layer: Mapped[str] = mapped_column(String(20), nullable=False)
    scene: Mapped[str] = mapped_column(String(100), nullable=False)
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    ground_truth: Mapped[dict] = mapped_column(JSON, nullable=False)
    expected_tools: Mapped[list] = mapped_column(JSON, nullable=False)
    forbidden_tools: Mapped[list] = mapped_column(JSON, nullable=False)
    expected_numbers: Mapped[list] = mapped_column(JSON, nullable=False)
    forbidden_phrases: Mapped[list] = mapped_column(JSON, nullable=False)
    must_ask_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    expected_verdict: Mapped[dict] = mapped_column(JSON, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    repeat: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    rubric_version: Mapped[str] = mapped_column(String(20), nullable=False, default="cs-v1")
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=func.current_timestamp(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "layer IN ('golden','adversarial','edge','regression')",
            name="ck_eval_case_layer",
        ),
        CheckConstraint("repeat > 0", name="ck_eval_case_repeat_pos"),
    )
