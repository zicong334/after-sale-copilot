"""report.py — 评测报告生成器(JSON + Markdown)。

报告口径(任务书 W4-A / PRD §6):
1. ``pass_rate`` 必须带分子/分母/n(不能只给一个百分比);
2. ``by_layer`` 按 golden / adversarial / edge / regression 分组;
3. ``critical_fail_count`` 单列(资损/越权/隐私失败,一票否决,不参与加权);
4. ``judge_agreement`` = 裁判通过结论与服务端硬断言一致的比例;
5. ``cost_per_case_usd`` 只算被评测对话的真实 token 成本,不含裁判;
6. ``p95_latency_ms`` 只算**用户等待时间**(对话轮 + 确认),不含裁判耗时;
7. ``failures`` 逐条可追溯;同类失败 ≥3 条标记为**成簇**(系统性缺陷)。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

#: 失败类型归类:用于 failures 展示与"成簇(同类≥3)"判定
FAILURE_TYPES = {
    # 与 hard_assert.Finding.check 的真实取值一一对应
    "input_error": "input_validation",
    "input_unexpected_error": "input_validation",
    "truncated": "input_validation",
    "original_length_min": "input_validation",
    "expected_tool": "tool_selection",
    "forbidden_tool": "forbidden_tool",
    "tool_error": "tool_error",
    "authz_denied": "authorization",
    "expected_number": "fact_mismatch",
    "must_contain": "fact_mismatch",
    "forbidden_phrase": "forbidden_phrase",
    "intent": "intent_mismatch",
    "handoff": "session_state",
    "ticket_created": "handoff_ticket",
    "ticket_status": "handoff_ticket",
    "handoff_reason": "handoff_reason",
    "refund_created": "write_safety",
    "refund_amount": "amount_mismatch",
    "refund_status": "write_safety",
    "max_refund_amount": "amount_mismatch",
    "pending_amount": "amount_mismatch",
    "confirm_asked": "confirmation",
    "write_without_confirmation": "confirmation",
    "session_status": "session_state",
    "ai_stops_reply": "handoff_stop",
    "memory_status": "memory_safety",
    "memory_executable": "memory_safety",
    "memory_text": "memory_safety",
    "orders_intact": "data_integrity",
    "no_write": "write_safety",
    "replay_no_duplicate_write": "idempotency",
    "idempotency_no_duplicate_write": "idempotency",
    "toctou_rejected": "toctou",
    "critical_refund_created": "write_safety",
    "critical_pricing_memory_active": "memory_safety",
    "authz_breach": "authorization",
}

#: 同一失败类型出现多少次算"成簇"(系统性缺陷)
CLUSTER_MIN = 3


def _p95(values: list[float]) -> float:
    """最近秩法 p95;空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return float(ordered[idx])


def _failed_items(hard: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容 HardResult.to_dict()(failures)/ 原始 checks 两种形态。"""
    items = hard.get("failures") if "failures" in hard else hard.get("checks")
    return [dict(item) for item in (items or []) if not item.get("ok", True)]


def classify_failure(hard: dict[str, Any], judge: dict[str, Any] | None = None) -> str:
    """把一次失败归到一个稳定类型,便于成簇统计。"""
    if hard.get("critical"):
        failed = _failed_items(hard)
        names = [str(c.get("check") or "") for c in failed]
        if "authz_denied" in names:
            return "authorization"
        forbidden = [n for n in names if n.startswith("forbidden_")]
        if forbidden:
            return "forbidden_action"
        return "critical"
    failed = _failed_items(hard)
    if failed:
        name = str(failed[0].get("check") or "").split(":")[0]
        return FAILURE_TYPES.get(name, name or "hard_assert")
    if judge and not judge.get("passed"):
        severity = str(judge.get("severity") or "none")
        return f"judge_{severity}" if severity != "none" else "judge_score"
    return "unknown"


def build_report(
    results: Iterable[dict[str, Any]],
    *,
    run_id: str,
    generated_at: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """由逐条 attempt 结果汇总出 6 指标报告。

    ``results`` 每项至少含:case_id, layer, attempt, task_success, critical,
    hard{checks, failures, critical}, judge{passed, weighted_score, severity,
    scored_by, model}, latency_ms, cost_usd, failure_type(可选)。
    """
    rows = list(results)
    attempts_total = len(rows)
    passed_attempts = sum(1 for r in rows if r.get("passed"))
    critical_rows = [r for r in rows if r.get("critical")]

    # by_layer:attempt 级 + case 级
    layers = ["golden", "adversarial", "edge", "regression"]
    by_layer: dict[str, Any] = {}
    for layer in layers:
        subset = [r for r in rows if r.get("layer") == layer]
        if not subset:
            by_layer[layer] = {
                "cases": 0,
                "attempts": 0,
                "passed_attempts": 0,
                "pass_rate": None,
                "critical_fail_count": 0,
            }
            continue
        case_ids = {r["case_id"] for r in subset}
        by_layer[layer] = {
            "cases": len(case_ids),
            "attempts": len(subset),
            "passed_attempts": sum(1 for r in subset if r.get("passed")),
            "pass_rate": round(
                sum(1 for r in subset if r.get("passed")) / len(subset), 4
            ),
            "critical_fail_count": sum(1 for r in subset if r.get("critical")),
        }

    # case 级:所有 attempt 都通过才算该 case 通过
    per_case: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        per_case.setdefault(str(r["case_id"]), []).append(r)
    cases_total = len(per_case)
    cases_passed = sum(1 for rs in per_case.values() if all(x.get("passed") for x in rs))

    # 裁判一致率:仅统计有裁判结论的 attempt
    judged = [r for r in rows if r.get("judge") is not None]
    agree = sum(
        1
        for r in judged
        if bool(r["judge"].get("passed")) == bool(r.get("task_success"))
    )
    judge_agreement = round(agree / len(judged), 4) if judged else None

    costs = [float(r.get("cost_usd") or 0.0) for r in rows]
    latencies = [float(r.get("latency_ms") or 0) for r in rows]

    failures: list[dict[str, Any]] = []
    for r in rows:
        if r.get("passed"):
            continue
        hard = dict(r.get("hard") or {})
        judge = r.get("judge")
        ftype = str(r.get("failure_type") or classify_failure(hard, judge))
        failed_checks = [
            {
                "check": c.get("check"),
                "detail": c.get("detail"),
                "severity": c.get("severity"),
            }
            for c in _failed_items(hard)
        ]
        failures.append(
            {
                "case_id": r["case_id"],
                "layer": r.get("layer"),
                "attempt": r.get("attempt"),
                "type": ftype,
                "critical": bool(r.get("critical")),
                "task_success": bool(r.get("task_success")),
                "judge_passed": bool(judge.get("passed")) if judge else None,
                "judge_score": judge.get("weighted_score") if judge else None,
                "detail": failed_checks[:4],
                "latency_ms": r.get("latency_ms"),
            }
        )

    # 成簇:同一类型 ≥ CLUSTER_MIN 次
    counter = Counter(f["type"] for f in failures)
    clusters = [
        {
            "type": ftype,
            "count": count,
            "case_ids": sorted({f["case_id"] for f in failures if f["type"] == ftype}),
            "systematic": count >= CLUSTER_MIN,
        }
        for ftype, count in counter.most_common()
    ]

    report: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": generated_at,
        "meta": {
            "mode": "mock",
            "assumptions": "FakeLLM + FakeJudge(MOCK):基准值,不是真模型能力",
            **(meta or {}),
        },
        "eval_set_version": (meta or {}).get("eval_set_version"),
        "rubric_version": (meta or {}).get("rubric_version"),
        "metrics": {
            "pass_rate": {
                "value": round(passed_attempts / attempts_total, 4)
                if attempts_total
                else None,
                "passed": passed_attempts,
                "denominator": attempts_total,
                "n": attempts_total,
                "cases_passed": cases_passed,
                "cases_total": cases_total,
            },
            "by_layer": by_layer,
            "critical_fail_count": len(critical_rows),
            "judge_agreement": {
                "value": judge_agreement,
                "agreed": agree,
                "judged": len(judged),
            },
            "cost_per_case_usd": {
                "mean": round(mean(costs), 8) if costs else 0.0,
                "total": round(sum(costs), 8),
                "n": len(costs),
            },
            "p95_latency_ms": {
                "value": _p95(latencies),
                "n": len(latencies),
                "scope": "user_wait_only(对话轮+确认,不含裁判)",
            },
        },
        "failures": failures,
        "clusters": clusters,
        "counts": {
            "attempts_failed": attempts_total - passed_attempts,
            "critical_failures": len(critical_rows),
        },
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    """人读版摘要(与 JSON 同源,不重复计算)。"""
    m = report.get("metrics") or {}
    pr = m.get("pass_rate") or {}
    lines = [
        f"# 评测报告 {report.get('run_id')}",
        "",
        f"- 生成时间:{report.get('generated_at')}",
        f"- 模式:{'MOCK(FakeLLM + FakeJudge)' if report.get('meta', {}).get('mode') == 'mock' else report.get('meta', {}).get('mode')}",
        f"- eval_set:{report.get('eval_set_version')} / rubric:{report.get('rubric_version')}",
        "",
        "## 6 指标",
        "",
        f"- pass_rate:**{pr.get('value')}**({pr.get('passed')}/{pr.get('denominator')} attempt;"
        f"case 级 {pr.get('cases_passed')}/{pr.get('cases_total')})",
        f"- critical_fail_count:**{m.get('critical_fail_count')}**",
        f"- judge_agreement:{(m.get('judge_agreement') or {}).get('value')}"
        f"({(m.get('judge_agreement') or {}).get('agreed')}/{(m.get('judge_agreement') or {}).get('judged')})",
        f"- cost_per_case_usd:{(m.get('cost_per_case_usd') or {}).get('mean')}",
        f"- p95_latency_ms:{(m.get('p95_latency_ms') or {}).get('value')}"
        f"(scope: {(m.get('p95_latency_ms') or {}).get('scope')})",
        "",
        "## 分层",
        "",
        "| layer | cases | attempts | passed | pass_rate | critical |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for layer, v in (m.get("by_layer") or {}).items():
        lines.append(
            f"| {layer} | {v.get('cases')} | {v.get('attempts')} | "
            f"{v.get('passed_attempts')} | {v.get('pass_rate')} | {v.get('critical_fail_count')} |"
        )
    lines += ["", "## 失败成簇(同类 ≥3 为系统性)", ""]
    clusters = report.get("clusters") or []
    if not clusters:
        lines.append("无失败。")
    else:
        lines += ["| type | count | systematic | case_ids |", "| --- | --- | --- | --- |"]
        for c in clusters:
            lines.append(
                f"| {c['type']} | {c['count']} | {'是' if c['systematic'] else '否'} | "
                f"{', '.join(c['case_ids'])} |"
            )
    lines += ["", "## failures(逐条)", ""]
    failures = report.get("failures") or []
    if not failures:
        lines.append("无。")
    else:
        lines += [
            "| case_id | attempt | type | critical | task_success | judge |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for f in failures:
            lines.append(
                f"| {f['case_id']} | {f['attempt']} | {f['type']} | "
                f"{f['critical']} | {f['task_success']} | {f['judge_passed']} |"
            )
    return "\n".join(lines) + "\n"


def write_report(
    report: dict[str, Any], out_dir: str | Path
) -> tuple[Path, Path]:
    """写 ``<out_dir>/<run_id>.json`` 与 ``.md``,返回两个路径。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    run_id = str(report.get("run_id") or "run")
    json_path = target / f"{run_id}.json"
    md_path = target / f"{run_id}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path
