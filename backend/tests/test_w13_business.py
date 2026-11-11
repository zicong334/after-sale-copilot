"""test_w13_business.py — W13 业务正确性复现测试(ISS-05 / ISS-06 / ISS-21 / ISS-58)。

方法铁律:每条先写复现测试(红)→修(绿)。
本文件只做"行为断言",不复述实现细节;缺陷证据写在断言注释里。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import guardrails
from app.graph import runner
from app.graph.build import DEFAULT_CHECKPOINT_PATH
from app.models import ChatSession, Ticket
from app.tools import policy as policy_tool
from app.trace import new_trace_id
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

# 评测用例 CS-REFUND-005 的真实输入(只读引用,不改 data/eval/)
CS005_TEXT = "我实付了 399，但是你们按 499 给我算的，不然我就投诉"
CS005_CONTEXT = {
    "order_id": "seed-order-019",
    "claimed_refund_cents": 49900,
    "claimed_paid_cents": 39900,
}

CLAUSE_CODE_RE = re.compile(r"\b(?:RP|LP)-\d{2}\b")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()
    yield


@pytest.fixture()
def db(engine, seeded):
    with Session(engine) as session:
        yield session


@pytest.fixture()
def ckpt(tmp_dir) -> Path:
    return tmp_dir / "checkpoints.db"


def new_row(db: Session, customer_id: str = "seed-cust-001") -> ChatSession:
    row = ChatSession(merchant_id=MERCHANT_ID, customer_id=customer_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def run_turn(db: Session, row: ChatSession, text: str, *, ckpt: Path, context: dict | None = None):
    return runner.run(
        row.id,
        text,
        session=db,
        context=context,
        trace_id=new_trace_id(),
        checkpoint_path=ckpt,
    )


def count_rows(db: Session, model) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------------------
# ISS-05:未发货/英文枚举 → 中文状态 + 未发货引导
# ---------------------------------------------------------------------------


def test_iss05_check_return_created_has_no_english_enum_and_has_guidance():
    """缺陷:policy._check_return 直吐英文枚举 created/delivered,并生硬驳回未发货订单。"""
    out = policy_tool._check_return("created", "七天无理由", 0)
    reason = str(out["reason"])
    assert out["eligible"] is False
    assert "created" not in reason, f"对客文案泄漏英文枚举: {reason}"
    assert "delivered" not in reason, f"对客文案泄漏英文枚举: {reason}"
    assert "待付款" in reason or "待发货" in reason, f"未发货订单应给中文状态: {reason}"
    assert "取消" in reason and "退款" in reason, f"未发货应引导取消退款: {reason}"
    assert "核对" in reason or "订单号" in reason, f"未发货应引导核对订单号: {reason}"


def test_iss05_check_return_shipped_status_is_chinese():
    out = policy_tool._check_return("shipped", "七天无理由", 0)
    reason = str(out["reason"])
    assert "shipped" not in reason
    assert "运输中" in reason


def test_iss05_graph_reply_for_created_order_has_no_english_enum(db, ckpt):
    """seed-order-001 = created(未发货);回复不得出现 created/delivered。"""
    row = new_row(db, "seed-cust-001")
    result = run_turn(
        db,
        row,
        "订单 seed-order-001 我要退货",
        ckpt=ckpt,
        context={"order_id": "seed-order-001"},
    )
    assert "created" not in result.reply, f"回复泄漏英文枚举: {result.reply}"
    assert "delivered" not in result.reply, f"回复泄漏英文枚举: {result.reply}"
    assert "待付款" in result.reply or "待发货" in result.reply, result.reply


def test_iss05_graph_reply_for_delivered_order_uses_chinese_label(db, ckpt):
    row = new_row(db, "seed-cust-009")
    result = run_turn(
        db,
        row,
        "订单 seed-order-019 我要退货",
        ckpt=ckpt,
        context={"order_id": "seed-order-019", "days_since_delivery": 9},
    )
    assert "delivered" not in result.reply, f"回复泄漏英文枚举: {result.reply}"
    assert "已签收" in result.reply, result.reply


# ---------------------------------------------------------------------------
# ISS-06:一句话"人工客服"必须直接挂起确认卡
# ---------------------------------------------------------------------------


def test_iss06_single_人工客服_message_raises_confirm_card(db, ckpt):
    """缺陷:输入"人工客服"未触发转人工确认卡,需二次输入。"""
    row = new_row(db)
    result = run_turn(db, row, "人工客服", ckpt=ckpt)
    assert result.intent == "handoff", result.intent
    assert result.pending_confirm is not None, "必须直接挂起转人工确认卡"
    assert result.pending_confirm.get("what")
    assert result.session_status == "handed_off"


# ---------------------------------------------------------------------------
# ISS-21:对客文本禁止出现内部条款代号 RP-xx / LP-xx
# ---------------------------------------------------------------------------


def test_iss21_sanitize_customer_text_replaces_clause_codes():
    out = guardrails.sanitize_customer_text("依据条款 RP-06 与 LP-04 的规定")
    assert "RP-06" not in out
    assert "LP-04" not in out
    assert "退款到账时效" in out
    assert "物流破损" in out


def test_iss21_policy_answer_reply_has_no_clause_codes(db, ckpt):
    row = new_row(db)
    result = run_turn(db, row, "退货的话，来回运费是谁出？", ckpt=ckpt)
    assert not CLAUSE_CODE_RE.search(result.reply), f"对客回复泄漏条款代号: {result.reply}"


# ---------------------------------------------------------------------------
# ISS-58:投诉/风险升级词优先判 handoff,原因映射 complaint
# ---------------------------------------------------------------------------


def test_iss58_complaint_amount_dispute_maps_to_complaint(db, ckpt):
    """CS-REFUND-005:金额争议叠加"投诉"→ intent=handoff 且 handoff_reason=complaint。"""
    row = new_row(db, "seed-cust-009")
    result = run_turn(db, row, CS005_TEXT, ckpt=ckpt, context=dict(CS005_CONTEXT))
    assert result.intent == "handoff", result.intent
    assert result.pending_confirm is not None
    assert [c["name"] for c in result.tool_calls] == ["shop__get_order", "handoff__create_ticket"], [
        c["name"] for c in result.tool_calls
    ]

    tickets_before = count_rows(db, Ticket)
    runner.resume(
        row.id,
        "确认",
        session=db,
        session_row=row,
        checkpoint_path=ckpt,
    )
    assert count_rows(db, Ticket) == tickets_before + 1
    ticket = db.execute(select(Ticket).order_by(Ticket.created_at.desc())).scalars().first()
    assert ticket is not None
    assert ticket.handoff_reason == "complaint", f"期望 complaint,实际 {ticket.handoff_reason}"
