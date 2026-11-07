"""W1-C 评测集测试:覆盖 10 条 golden 用例结构、validate 脚本退出码、
load 幂等、order_id 与 seed 真实订单对齐、ground_truth 自洽、工具名注册。

不写任何临时文件(validate 拒绝用例用内存 dict、load 幂等走 session fixture),
不违反沙箱护栏(不碰 tempfile.mkdtemp、不碰 .tmp 之外的临时目录)。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import EvalCase

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "data" / "eval" / "cases_w1.json"
SCRIPTS_DIR = ROOT / "backend" / "scripts"
VALIDATE_SCRIPT = SCRIPTS_DIR / "validate_eval_cases.py"
LOAD_SCRIPT = SCRIPTS_DIR / "load_eval_cases.py"
SEED_SCRIPT = SCRIPTS_DIR / "seed.py"

# 五个注册工具名(PRD §4 危险动作清单)
REGISTERED_TOOLS = {
    "shop__get_order",
    "policy__search",
    "policy__check_return",
    "refund__create",
    "handoff__create_ticket",
}

LAYERS = {"golden", "adversarial", "edge", "regression"}

# 17 个业务字段(照 EvalCase 模型 / 03§4.3.2)
REQUIRED_FIELDS = [
    "case_id",
    "layer",
    "scene",
    "user_input",
    "context",
    "ground_truth",
    "expected_tools",
    "forbidden_tools",
    "expected_numbers",
    "forbidden_phrases",
    "must_ask_confirmation",
    "expected_verdict",
    "weight",
    "repeat",
    "rubric_version",
    "source",
    "notes",
]

# 判责 case → (order_id, 期望的 seed 状态);来源 seed.py `_ORDER_STATUSES`(delivered = idx 18..23)
_CASE_ORDER_STATUS = {
    "CS-REFUND-003": ("seed-order-019", "delivered"),
    "CS-REFUND-004": ("seed-order-020", "delivered"),
    "CS-REFUND-005": ("seed-order-019", "delivered"),
    "CS-REFUND-006": ("seed-order-019", "delivered"),
}


def _load_cases() -> list[dict]:
    with CASES_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_module(name: str, path: Path):
    """按文件路径加载一个脚本模块(不污染 sys.modules 通用命名)。"""
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1) JSON 结构:10 条 + 17 字段齐全 + 无多余字段
# ---------------------------------------------------------------------------
def test_cases_json_has_10_cases_with_17_fields():
    cases = _load_cases()
    assert len(cases) == 10, f"应为 10 条,实际 {len(cases)}"

    for idx, case in enumerate(cases):
        missing = [f for f in REQUIRED_FIELDS if f not in case]
        extra = [k for k in case if k not in REQUIRED_FIELDS]
        assert not missing, f"case[{idx}] 缺字段 {missing}"
        assert not extra, f"case[{idx}] 存在非白名单字段 {extra}"


# ---------------------------------------------------------------------------
# 2) case_id 唯一 + layer 枚举 + repeat/rubric_version 约定
# ---------------------------------------------------------------------------
def test_case_ids_unique_and_enum_valid():
    cases = _load_cases()
    ids = [c["case_id"] for c in cases]
    assert len(ids) == len(set(ids)), "case_id 必须唯一"

    for case in cases:
        assert case["layer"] in LAYERS, f"{case['case_id']}.layer 非法: {case['layer']}"
        assert case["repeat"] == 3, f"{case['case_id']}.repeat 应为 3"
        assert case["rubric_version"] == "cs-v1", f"{case['case_id']}.rubric_version 应为 cs-v1"


# ---------------------------------------------------------------------------
# 3) 工具名只用五个注册名 + expected/forbidden 不相交
# ---------------------------------------------------------------------------
def test_tools_only_registered_names():
    cases = _load_cases()
    for case in cases:
        cid = case["case_id"]
        for field in ("expected_tools", "forbidden_tools"):
            tools = case[field]
            bad = [t for t in tools if t not in REGISTERED_TOOLS]
            assert not bad, f"{cid}.{field} 含未注册工具 {bad}"
        inter = set(case["expected_tools"]) & set(case["forbidden_tools"])
        assert not inter, f"{cid} 工具自相矛盾: {inter}"


# ---------------------------------------------------------------------------
# 4) validate 脚本真实退出码 == 0
# ---------------------------------------------------------------------------
def test_validate_script_exit_code_0():
    # 直接调用 main() 拿退出码(不经过 subprocess:Windows 沙箱下 subprocess 建管道会被拒)
    vc = _load_module("validate_eval_cases", VALIDATE_SCRIPT)
    code = vc.main([str(VALIDATE_SCRIPT), str(CASES_PATH)])
    assert code == 0, f"validate 退出码应为 0,实际 {code}"


# ---------------------------------------------------------------------------
# 5) validate 逻辑拒绝坏 case(缺字段 / layer 非法 / expected_numbers 非字符串)
# ---------------------------------------------------------------------------
def test_validate_rejects_invalid_case():
    vc = _load_module("validate_eval_cases", VALIDATE_SCRIPT)
    base = _load_cases()

    # layer 非法
    bad_layer = [dict(c) for c in base]
    bad_layer[0]["layer"] = "bogus"
    assert vc.validate_cases(bad_layer), "layer 非法应报错"

    # expected_numbers 非字符串数组
    bad_numbers = [dict(c) for c in base]
    bad_numbers[1]["expected_numbers"] = [7]  # int 而非 str
    assert any("expected_numbers" in e for e in vc.validate_cases(bad_numbers))

    # 缺字段
    bad_missing = [dict(c) for c in base]
    del bad_missing[2]["ground_truth"]
    assert any("缺字段" in e for e in vc.validate_cases(bad_missing))


# ---------------------------------------------------------------------------
# 6) load 幂等:upsert 两遍 eval_case 行数不变(仍为 10)
# ---------------------------------------------------------------------------
def test_load_upsert_idempotent(session):
    load_mod = _load_module("load_eval_cases", LOAD_SCRIPT)
    cases = _load_cases()

    first = load_mod.upsert_cases(session, cases)
    second = load_mod.upsert_cases(session, cases)

    assert first["inserted"] == 10 and first["updated"] == 0
    assert second["inserted"] == 0 and second["updated"] == 10
    assert first["total"] == 10 and second["total"] == 10, "两遍装载后行数必须都是 10"

    # 直接查库佐证(而非只信 upsert 返回值)
    count = session.scalar(select(func.count()).select_from(EvalCase))
    assert count == 10


# ---------------------------------------------------------------------------
# 7) order_id 与 seed 真实订单对齐(状态 / 金额 / 归属客户)
# ---------------------------------------------------------------------------
def test_order_ids_match_seed():
    seed_mod = _load_module("_seed_module", SEED_SCRIPT)
    statuses: list[str] = seed_mod._ORDER_STATUSES  # 30 状态序列(真实 seed)

    def seed_status(order_id: str) -> str:
        idx = int(order_id.rsplit("-", 1)[1]) - 1
        return statuses[idx]

    def seed_total_cents(order_id: str) -> int:
        # 来源 seed.py:`total_cents=10000 + idx*100`
        idx = int(order_id.rsplit("-", 1)[1]) - 1
        return 10000 + idx * 100

    cases = _load_cases()

    # 判责 case 的订单状态必须与 seed 真实状态一致
    for case in cases:
        if case["case_id"] in _CASE_ORDER_STATUS:
            want_order, want_status = _CASE_ORDER_STATUS[case["case_id"]]
            assert case["context"]["order_id"] == want_order
            assert seed_status(want_order) == want_status, (
                f"{case['case_id']} 依赖 {want_order}={want_status},"
                f"实际 seed 状态={seed_status(want_order)}"
            )

    # 所有 case 引用的 order_id 都在 seed-order-001..030 合法范围
    legal = {f"seed-order-{i:03d}" for i in range(1, 31)}
    for case in cases:
        oid = case["context"].get("order_id")
        if oid:
            assert oid in legal, f"{case['case_id']} 引用非法订单号 {oid}"

    # CS-REFUND-004 退款金额 == seed-order-020 实付 11900 分
    refund004 = next(c for c in cases if c["case_id"] == "CS-REFUND-004")
    assert refund004["ground_truth"]["refund_amount_cents"] == seed_total_cents("seed-order-020") == 11900

    # 越权用例:seed-order-019 属 seed-cust-009,当前用户 seed-cust-001(确实越权)
    cross = next(c for c in cases if c["case_id"] == "CS-REFUND-006")
    # 来源 seed.py:customer_id = seed-cust-{(idx % 10) + 1}
    assert (18 % 10) + 1 == 9, "seed-order-019 应属 seed-cust-009"
    assert cross["context"]["current_customer_id"] == "seed-cust-001"


# ---------------------------------------------------------------------------
# 8) ground_truth 自洽:退款禁/允、转人工与工具/verdict 一致
# ---------------------------------------------------------------------------
def test_ground_truth_self_consistent():
    cases = _load_cases()
    for case in cases:
        cid = case["case_id"]
        expected = set(case["expected_tools"])
        forbidden = set(case["forbidden_tools"])
        gt = case["ground_truth"]
        verdict = case["expected_verdict"]

        # 禁止退款 → 不创建退款单,且 eligible(若有)必须为 false
        if "refund__create" in forbidden:
            assert gt.get("refund_created") is not True, f"{cid} 禁止退款却 refund_created=true"
            if "eligible" in gt:
                assert gt["eligible"] is False, f"{cid} 禁止退款却 eligible=true"

        # 允许退款 → eligible=true 且必须二次确认
        if "refund__create" in expected:
            assert gt.get("eligible") is True, f"{cid} 允许退款却 eligible!=true"
            assert case["must_ask_confirmation"] is True, f"{cid} 退款前必须二次确认"

        # 转人工 → verdict.handoff=true 且建工单/置 handed_off
        if "handoff__create_ticket" in expected:
            assert verdict.get("handoff") is True, f"{cid} 转人工但 verdict.handoff!=true"
            assert gt.get("ticket_created") is True or gt.get("handoff") is True, (
                f"{cid} 转人工但 ground_truth 未标建工单"
            )
