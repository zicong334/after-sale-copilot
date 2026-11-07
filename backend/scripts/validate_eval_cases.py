"""validate_eval_cases.py — 纯校验脚本(不连库)。

读 cases JSON → 逐条校验 17 字段齐全、枚举合法、工具只用五个注册名、
expected_numbers 是字符串数组、context.order_id 与 seed 订单对得上。

用法:
    python backend/scripts/validate_eval_cases.py [data/eval/cases_w1.json]

退出码:0 = 全部通过;1 = 存在错误(错误明细打印到 stderr)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 五个注册工具名(PRD §4 第 1 项「危险动作清单」)
REGISTERED_TOOLS = {
    "shop__get_order",
    "policy__search",
    "policy__check_return",
    "refund__create",
    "handoff__create_ticket",
}

# layer 枚举(eval_case 表 CHECK 约束一致)
LAYERS = {"golden", "adversarial", "edge", "regression"}

# 17 个业务字段(照 03§4.3.2 单条 case 示例,一个不少)
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

# W1-B seed 的 30 个真实订单号(seed.py:`seed-order-{idx+1:03d}`,idx=0..29)
SEED_ORDER_IDS = {f"seed-order-{i:03d}" for i in range(1, 31)}

# 允许的额外字段(不在 17 字段内,若出现则报错,防止混入未知字段)
_ALLOWED_EXTRA_FIELDS: set[str] = set()


def validate_cases(cases) -> list[str]:
    """校验用例列表,返回错误信息列表(空列表 = 通过)。"""
    errors: list[str] = []

    if not isinstance(cases, list):
        return ["顶层必须是 JSON 数组"]

    if len(cases) != 10:
        errors.append(f"用例条数必须为 10,实际 {len(cases)}")

    seen_case_ids: set[str] = set()
    for idx, case in enumerate(cases):
        prefix = f"case[{idx}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} 必须是对象")
            continue

        # 1) 17 字段齐全 + 无多余字段
        missing = [f for f in REQUIRED_FIELDS if f not in case]
        if missing:
            errors.append(f"{prefix} 缺字段: {missing}")
        extra = [k for k in case if k not in REQUIRED_FIELDS]
        if extra:
            errors.append(f"{prefix} 存在非白名单字段: {extra}")

        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{prefix}.case_id 必须是非空字符串")
        else:
            if case_id in seen_case_ids:
                errors.append(f"{prefix}.case_id 重复: {case_id}")
            seen_case_ids.add(case_id)

        # 2) layer 枚举
        layer = case.get("layer")
        if layer not in LAYERS:
            errors.append(
                f"{prefix}.layer 必须是 {sorted(LAYERS)} 之一,实际 {layer!r}"
            )

        # 3) 字符串字段
        for f in ("scene", "user_input", "source"):
            v = case.get(f)
            if not isinstance(v, str) or not v:
                errors.append(f"{prefix}.{f} 必须是非空字符串")

        # 4) JSON 对象字段
        for f in ("context", "ground_truth", "expected_verdict"):
            v = case.get(f)
            if not isinstance(v, dict):
                errors.append(f"{prefix}.{f} 必须是对象,实际 {type(v).__name__}")

        # 5) 列表字段(字符串数组)
        for f in ("expected_tools", "forbidden_tools", "forbidden_phrases"):
            v = case.get(f)
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                errors.append(f"{prefix}.{f} 必须是字符串数组")

        # 6) expected_numbers 必须是字符串数组(任务书硬要求)
        en = case.get("expected_numbers")
        if not isinstance(en, list) or not all(isinstance(x, str) for x in en):
            errors.append(
                f"{prefix}.expected_numbers 必须是字符串数组,实际 {en!r}"
            )

        # 7) must_ask_confirmation 布尔
        if not isinstance(case.get("must_ask_confirmation"), bool):
            errors.append(f"{prefix}.must_ask_confirmation 必须是布尔值")

        # 8) weight / repeat / rubric_version
        weight = case.get("weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            errors.append(f"{prefix}.weight 必须是正数,实际 {weight!r}")

        repeat = case.get("repeat")
        if repeat != 3:
            errors.append(f"{prefix}.repeat 必须为 3,实际 {repeat!r}")

        rv = case.get("rubric_version")
        if rv != "cs-v1":
            errors.append(f"{prefix}.rubric_version 必须为 \"cs-v1\",实际 {rv!r}")

        # 9) notes 允许 str 或 None
        if not isinstance(case.get("notes"), (str, type(None))):
            errors.append(f"{prefix}.notes 必须是字符串或 null")

        # 10) 工具只用五个注册名
        for f in ("expected_tools", "forbidden_tools"):
            tools = case.get(f)
            if isinstance(tools, list):
                bad = [t for t in tools if t not in REGISTERED_TOOLS]
                if bad:
                    errors.append(
                        f"{prefix}.{f} 含未注册工具 {bad};注册工具 = {sorted(REGISTERED_TOOLS)}"
                    )

        # 11) expected 与 forbidden 不得交集(自洽)
        et = case.get("expected_tools") or []
        ft = case.get("forbidden_tools") or []
        if isinstance(et, list) and isinstance(ft, list):
            inter = set(et) & set(ft)
            if inter:
                errors.append(f"{prefix} 工具自相矛盾(既 expected 又 forbidden): {sorted(inter)}")

        # 12) context.order_id 必须与 seed 真实订单对上
        ctx = case.get("context")
        if isinstance(ctx, dict) and ctx.get("order_id"):
            oid = ctx["order_id"]
            if not isinstance(oid, str) or oid not in SEED_ORDER_IDS:
                errors.append(
                    f"{prefix}.context.order_id 必须是 seed 订单(seed-order-001..030),实际 {oid!r}"
                )

    return errors


def _load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _validate_for(data: object) -> list[str]:
    """按用例集版本选择校验规则。

    W1 的 10 条 golden 继续走本文件内冻结的 ``validate_cases``(10 条规则);
    W4-A 起的 48 条(``cases_v1.json``,rubric cs-v2)统一走
    ``app.eval.validate_cases``(48 条 + 配额 + 必造用例 + 17 字段),
    避免两套规则长期漂移。
    """
    if isinstance(data, list) and len(data) != 10:
        backend_dir = str(Path(__file__).resolve().parents[1])
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)
        from app.eval.validate_cases import validate_cases as validate_v1_cases

        return validate_v1_cases(data)
    return validate_cases(data)


def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        path = Path(argv[1])
    else:
        path = Path("data/eval/cases_w1.json")

    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    try:
        data = _load_json(path)
    except json.JSONDecodeError as exc:
        print(f"JSON 解析失败: {exc}", file=sys.stderr)
        return 1

    errors = _validate_for(data)
    if errors:
        print(f"校验失败:共 {len(errors)} 个错误", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    count = len(data) if isinstance(data, list) else 0
    print(f"OK: {count} cases 全部通过校验(17 字段齐全、枚举合法、工具名合法、order_id 与 seed 对齐)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
