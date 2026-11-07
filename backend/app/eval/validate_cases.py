"""48 条评测用例集校验器(W4-A)。

背景:W1-C 的 ``backend/scripts/validate_eval_cases.py`` 冻结为 10 条规则
(``len(cases) != 10`` / ``rubric_version == cs-v1``),不能用于 v1 的 48 条集合。
本模块是 W4-A 白名单内的新校验器,``run_eval.py`` 在装载用例时调用它;
W1-C 脚本仅新增一条分支(10 条走旧规则、非 10 条委托本模块),其余保持原样。

用法::

    python -m app.eval.validate_cases data/eval/cases_v1.json
    # 或 from app.eval.validate_cases import validate_file

退出码:0 = 通过;1 = 有校验错误(逐条打印)。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REQUIRED_FIELDS: tuple[str, ...] = (
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
)

LAYERS: tuple[str, ...] = ("golden", "adversarial", "edge", "regression")
QUOTAS: dict[str, int] = {
    "golden": 19,
    "adversarial": 14,
    "edge": 10,
    "regression": 5,
}
TOTAL_CASES = sum(QUOTAS.values())

TOOL_NAMES: frozenset[str] = frozenset(
    {
        "shop__get_order",
        "policy__search",
        "policy__check_return",
        "refund__create",
        "handoff__create_ticket",
    }
)

GROUND_TRUTH_KEYS: frozenset[str] = frozenset(
    {
        "intent",
        "clause_id",
        "answer_fact",
        "must_contain",
        "handoff",
        "handoff_reason",
        "ticket_created",
        "ticket_status",
        "session_status",
        "ai_stops_reply",
        "refund_created",
        "refund_amount_cents",
        "refund_status",
        "eligible",
        "reason",
        "input_error",
        "truncated",
        "original_length_min",
        "tool_error",
        "memory_status",
        "memory_executable",
        "memory_text_contains",
        "orders_intact",
        "no_write",
        "repeat_turns",
        "approve",
        "followup_input",
        "replay_resume",
        "idempotency_retry",
        "toctou_tamper_total_cents",
        "max_refund_amount_cents",
        "pending_amount_cents",
        "critical_if_refund_created",
        "critical_if_privacy_leak",
        "critical_if_pricing_memory_active",
        "critical_if_unauthorized_access",
    }
)

_ERROR_CODES = frozenset(
    {
        "VALIDATION_ERROR",
        "NOT_FOUND_OR_FORBIDDEN",
        "BUSINESS_REJECT",
        "CONFIRM_NOT_PENDING",
        "TOOL_ERROR",
    }
)

_SEED_ORDER_RE = re.compile(r"^seed-order-0(0[1-9]|[12][0-9]|30)$")

# 必造对抗用例的语义锚点(case_id 约定,保证配额不"注水")
REQUIRED_ADVERSARIAL_IDS: tuple[str, ...] = (
    "CS-ADV-001",  # 注入改价
    "CS-ADV-002",  # 越权查单
    "CS-ADV-003",  # 记忆污染
    "CS-ADV-004",  # 金额篡改
    "CS-ADV-005",  # 确认卡钓鱼
)

REQUIRED_EDGE_IDS: tuple[str, ...] = (
    "CS-EDGE-001",  # 空输入
    "CS-EDGE-002",  # 超长 4000+
    "CS-EDGE-003",  # 纯图片 URL
    "CS-EDGE-004",  # 同问题 x3
    "CS-EDGE-005",  # 中英混杂
)

REQUIRED_REGRESSION_IDS: tuple[str, ...] = (
    "CS-REG-001",  # 转人工后 AI 回复
    "CS-REG-002",  # 恢复重放
    "CS-REG-003",  # 幂等重复建单
    "CS-REG-004",  # TOCTOU
    "CS-REG-005",  # 期望-上下文矛盾(CS-POLICY-004 类)
)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_cases(cases: Any) -> list[str]:
    """校验用例集,返回错误信息列表(空列表 = 通过)。"""
    errors: list[str] = []
    if not isinstance(cases, list):
        return ["cases 必须是 JSON 数组"]
    if len(cases) != TOTAL_CASES:
        errors.append(f"用例总数应为 {TOTAL_CASES},实际 {len(cases)}")

    counts = {layer: 0 for layer in LAYERS}
    seen_ids: dict[str, int] = {}
    indexed: dict[str, dict[str, Any]] = {}

    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{where}: 必须是对象")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in case]
        if missing:
            errors.append(f"{where}: 缺少 17 字段中的 {missing}")
        extra = sorted(set(case) - set(REQUIRED_FIELDS))
        if extra:
            errors.append(f"{where}: 出现 17 字段之外的键 {extra}")

        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{where}: case_id 必须是非空字符串")
            continue
        if case_id in seen_ids:
            errors.append(f"{where}: case_id 重复 {case_id}")
        seen_ids[case_id] = index
        indexed[case_id] = case
        where = f"{case_id}"

        layer = case.get("layer")
        if layer not in LAYERS:
            errors.append(f"{where}: layer 非法 {layer!r}")
        else:
            counts[layer] += 1

        if not isinstance(case.get("scene"), str) or not case.get("scene"):
            errors.append(f"{where}: scene 必须是非空字符串")
        if not isinstance(case.get("user_input"), str):
            errors.append(f"{where}: user_input 必须是字符串")
        if not isinstance(case.get("context"), dict):
            errors.append(f"{where}: context 必须是对象")
        if not isinstance(case.get("ground_truth"), dict):
            errors.append(f"{where}: ground_truth 必须是对象")
        else:
            bad_keys = sorted(set(case["ground_truth"]) - GROUND_TRUTH_KEYS)
            if bad_keys:
                errors.append(f"{where}: ground_truth 含未知键 {bad_keys}")

        expected = case.get("expected_tools")
        forbidden = case.get("forbidden_tools")
        if not _is_str_list(expected):
            errors.append(f"{where}: expected_tools 必须是字符串数组")
            expected = []
        if not _is_str_list(forbidden):
            errors.append(f"{where}: forbidden_tools 必须是字符串数组")
            forbidden = []
        for tool in [*expected, *forbidden]:
            if tool not in TOOL_NAMES:
                errors.append(f"{where}: 非法工具名 {tool!r}")
        overlap = sorted(set(expected) & set(forbidden))
        if overlap:
            errors.append(f"{where}: expected_tools 与 forbidden_tools 交集非空 {overlap}")

        if not _is_str_list(case.get("expected_numbers")):
            errors.append(f"{where}: expected_numbers 必须是字符串数组")
        if not _is_str_list(case.get("forbidden_phrases")):
            errors.append(f"{where}: forbidden_phrases 必须是字符串数组")

        if not isinstance(case.get("must_ask_confirmation"), bool):
            errors.append(f"{where}: must_ask_confirmation 必须是布尔值")

        verdict = case.get("expected_verdict")
        if not isinstance(verdict, dict):
            errors.append(f"{where}: expected_verdict 必须是对象")
        else:
            if not isinstance(verdict.get("task_success"), bool):
                errors.append(f"{where}: expected_verdict.task_success 必须是布尔值")
            if "handoff" in verdict and not isinstance(verdict["handoff"], bool):
                errors.append(f"{where}: expected_verdict.handoff 必须是布尔值")
            if "handoff" not in verdict and "handoff" not in case.get("ground_truth", {}):
                errors.append(f"{where}: handoff 期望缺失(expected_verdict/ground_truth)")

        weight = case.get("weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            errors.append(f"{where}: weight 必须是正数")
        repeat = case.get("repeat")
        if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat <= 0:
            errors.append(f"{where}: repeat 必须是正整数")
        if case.get("rubric_version") not in {"cs-v2", "cs-v1"}:
            errors.append(
                f"{where}: rubric_version 必须是 cs-v2(或兼容 cs-v1),实际 {case.get('rubric_version')!r}"
            )
        if not isinstance(case.get("source"), str) or not case.get("source"):
            errors.append(f"{where}: source 必须是非空字符串")
        if not isinstance(case.get("notes"), str):
            errors.append(f"{where}: notes 必须是字符串")

        context = case.get("context") if isinstance(case.get("context"), dict) else {}
        order_id = context.get("order_id")
        if order_id is not None and (
            not isinstance(order_id, str) or not _SEED_ORDER_RE.match(order_id)
        ):
            errors.append(f"{where}: context.order_id 必须是 seed-order-001..030,实际 {order_id!r}")
        attachments = context.get("attachments")
        if attachments is not None and not _is_str_list(attachments):
            errors.append(f"{where}: context.attachments 必须是字符串数组")

        gt = case.get("ground_truth") if isinstance(case.get("ground_truth"), dict) else {}
        tool_error = gt.get("tool_error")
        if tool_error is not None:
            if not isinstance(tool_error, dict):
                errors.append(f"{where}: ground_truth.tool_error 必须是对象")
            else:
                if tool_error.get("tool") not in TOOL_NAMES:
                    errors.append(f"{where}: tool_error.tool 非法")
                if tool_error.get("error_code") not in _ERROR_CODES:
                    errors.append(f"{where}: tool_error.error_code 非法 {tool_error.get('error_code')!r}")
        for key in ("refund_amount_cents", "max_refund_amount_cents", "pending_amount_cents"):
            value = gt.get(key)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                errors.append(f"{where}: ground_truth.{key} 必须是非负整数")

        if layer == "adversarial":
            if gt.get("refund_created") is True:
                errors.append(f"{where}: adversarial 用例不应期望创建退款")
            if not (
                gt.get("critical_if_refund_created")
                or gt.get("critical_if_privacy_leak")
                or gt.get("critical_if_pricing_memory_active")
                or gt.get("no_write")
            ):
                errors.append(f"{where}: adversarial 用例缺少 critical/no_write 风险标记")

    for layer, quota in QUOTAS.items():
        if counts[layer] != quota:
            errors.append(f"配额不符: {layer} 应为 {quota},实际 {counts[layer]}")

    for required in REQUIRED_ADVERSARIAL_IDS:
        case = indexed.get(required)
        if case is None:
            errors.append(f"缺少必造对抗用例 {required}")
        elif case.get("layer") != "adversarial":
            errors.append(f"{required}: layer 必须是 adversarial")
    for required in REQUIRED_EDGE_IDS:
        case = indexed.get(required)
        if case is None:
            errors.append(f"缺少必造边界用例 {required}")
        elif case.get("layer") != "edge":
            errors.append(f"{required}: layer 必须是 edge")
    for required in REQUIRED_REGRESSION_IDS:
        case = indexed.get(required)
        if case is None:
            errors.append(f"缺少必造回归用例 {required}")
        elif case.get("layer") != "regression":
            errors.append(f"{required}: layer 必须是 regression")

    return errors


def stats(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """返回配额/重复次数/权重等统计(供回执贴数据)。"""
    by_layer: dict[str, int] = {layer: 0 for layer in LAYERS}
    for case in cases:
        layer = case.get("layer")
        if layer in by_layer:
            by_layer[layer] += 1
    repeats = {case.get("repeat") for case in cases}
    rubric_versions = sorted({case.get("rubric_version") for case in cases})
    return {
        "total": len(cases),
        "by_layer": by_layer,
        "quotas": QUOTAS,
        "all_17_fields": all(
            all(field in case for field in REQUIRED_FIELDS) for case in cases
        ),
        "unique_case_ids": len({case.get("case_id") for case in cases}),
        "repeats": sorted(r for r in repeats if isinstance(r, int)),
        "rubric_versions": rubric_versions,
    }


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("cases 文件必须是 JSON 数组")
    return data


def validate_file(path: str | Path) -> tuple[list[str], dict[str, Any]]:
    cases = load_cases(path)
    return validate_cases(cases), stats(cases)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("用法: python -m app.eval.validate_cases <cases.json>", file=sys.stderr)
        return 2
    errors, summary = validate_file(argv[0])
    print(json.dumps({"ok": not errors, "stats": summary}, ensure_ascii=False, indent=2))
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[OK] cases_v1 校验通过")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
