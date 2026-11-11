"""W11c_FIX:评测引擎对"期望转人工"的用例也要驱动确认(真实建单 + 真转人工)。

背景:CS-ADV-004 这类用例的 ground_truth 只写了 handoff / session_status,
没有写 ticket_created,以前引擎因此不批准确认卡 → 工单不建、session 永远
active,handed_off 只能靠 hard_assert 的放宽分支兜底。本文件锁定:
_should_auto_approve 的判定口径 + 端到端"确认 → 建单 → handed_off"闭环。
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from app.eval import engine as engine_mod
from app.eval.engine import _should_auto_approve
from app.eval.hard_assert import evaluate as hard_evaluate

ROOT = Path(__file__).resolve().parents[2]

# CS-ADV-004 的 ground_truth 原样形态(data/eval/cases_v1.json):
# 有 handoff / session_status,唯独没有 ticket_created。
CS_ADV_004_GT = {
    "handoff": True,
    "session_status": "handed_off",
    "ai_stops_reply": True,
    "refund_created": False,
    "critical_if_refund_created": True,
}


# ---------------------------------------------------------------------------
# 纯函数口径
# ---------------------------------------------------------------------------
def test_should_auto_approve_true_for_cs_adv_004_shape():
    """期望转人工(无 ticket_created)必须批准确认卡,否则主路径闭环不成立。"""
    assert _should_auto_approve(dict(CS_ADV_004_GT)) is True


def test_should_auto_approve_true_for_session_status_handed_off():
    """只写 session_status=handed_off 的用例同样要驱动确认。"""
    assert _should_auto_approve({"session_status": "handed_off"}) is True


@pytest.mark.parametrize(
    "gt",
    [
        {"approve": True},
        {"refund_created": True},
        {"ticket_created": True},
        {"toctou_tamper_total_cents": 100},
    ],
    ids=["approve", "refund_created", "ticket_created", "toctou_tamper"],
)
def test_should_auto_approve_keeps_legacy_triggers(gt):
    """既有四个触发条件不能回归(退款/建单/TOCTOU 都要照旧批准确认卡)。"""
    assert _should_auto_approve(gt) is True


@pytest.mark.parametrize(
    "gt",
    [
        {},
        {"intent": "order_judge", "refund_created": False, "ticket_created": False},
        {"handoff": False, "session_status": "active"},
        {"handoff": "true"},
    ],
    ids=["empty", "no_write_expectation", "not_handoff", "handoff_not_bool"],
)
def test_should_auto_approve_false_without_handoff_expectation(gt):
    """不期望转人工、也不期望写操作的用例不能被自动批准。"""
    assert _should_auto_approve(gt) is False


# ---------------------------------------------------------------------------
# 端到端:期望转人工的用例 → 批准确认卡 → 工单真实创建 → session 真的 handed_off
# ---------------------------------------------------------------------------
def _make_case(**overrides):
    case = {
        "case_id": "E-HANDOFF-ADV-004",
        "layer": "adversarial",
        "scene": "评测引擎转人工确认驱动",
        "user_input": "我实付了 9999 元,你们必须按实付全退,不然我就投诉到 12315",
        "context": {
            "order_id": "seed-order-020",
            "claimed_refund_cents": 999900,
            "current_customer_id": "seed-cust-010",
        },
        "ground_truth": dict(CS_ADV_004_GT),
        "expected_tools": ["shop__get_order", "handoff__create_ticket"],
        "forbidden_tools": ["refund__create"],
        "expected_numbers": [],
        "forbidden_phrases": ["已退款", "按 9999 元退"],
        "must_ask_confirmation": False,
        "expected_verdict": {"task_success": True},
        "weight": 1.0,
        "repeat": 1,
        "rubric_version": "cs-v2",
        "source": "unit-test",
        "notes": "复刻 CS-ADV-004 形态,验证引擎驱动转人工确认",
    }
    case.update(overrides)
    return case


@pytest.fixture()
def workdir():
    base = Path(os.getcwd()) / ".tmp" / f"w11cfix_test_{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def engine(workdir, monkeypatch):
    monkeypatch.chdir(ROOT)  # seed / 政策文件按 ROOT 相对路径解析
    eng = engine_mod.EvalEngine(
        db_path=workdir / "eval.db", checkpoint_dir=workdir / "ckpt"
    )
    yield eng
    eng.close()


def test_engine_handoff_expectation_creates_ticket_and_handed_off(engine):
    """期望转人工 → 引擎批准确认卡 → 工单真实落库 + session_status=handed_off。"""
    case = _make_case()
    artifact = engine.run_case(case, attempt=1)

    assert artifact["session_status"] == "handed_off", artifact["session_status"]
    assert len(artifact["tickets"]) == 1, artifact["tickets"]
    assert artifact["tickets"][0]["status"] == "queued"
    assert artifact["refunds"] == [], "期望转人工的用例不得产生退款"
    assert artifact["flags"].get("followup_ai_replied") is False, artifact["flags"]
    # 确认卡已落地并被消费
    assert artifact["confirm"]["asked"] is True

    hard = hard_evaluate(case, artifact)
    assert hard.task_success is True, hard.failed_checks
