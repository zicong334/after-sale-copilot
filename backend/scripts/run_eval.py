#!/usr/bin/env python
"""run_eval.py — 一条命令跑完 W4-A 评测。

用法(在项目根执行):
    .venv/Scripts/python backend/scripts/run_eval.py --repeat 3
    .venv/Scripts/python backend/scripts/run_eval.py --smoke
    .venv/Scripts/python backend/scripts/run_eval.py --layer adversarial

流程:装载用例 → 校验(48 条/配额/17 字段)→ 逐条 × repeat 跑图(FakeLLM)
→ 三层判定(硬断言 / 裁判 / 报告汇总)→ 写 data/eval/reports/<run_id>.json+md
→ 把报告 JSON 原文打到 stdout(供 W4-C 对比基线)。

本阶段**不接真实 LLM**:LLM 用 FakeLLM、裁判用 FakeJudge,报告 meta 明确标注
mode=mock。W4-C 只需替换 judge client 与 LLM,统计口径不变。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.eval.engine import EvalEngine  # noqa: E402
from app.eval.hard_assert import evaluate as hard_evaluate  # noqa: E402
from app.eval.judge import build_judge  # noqa: E402
from app.eval.report import build_report, write_report  # noqa: E402
from app.eval.validate_cases import load_cases, stats, validate_cases  # noqa: E402

DEFAULT_CASES = ROOT / "data" / "eval" / "cases_v1.json"
DEFAULT_OUT = ROOT / "data" / "eval" / "reports"
DEFAULT_TMP = ROOT / ".tmp" / "run_eval"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="售后 Copilot 评测执行器(48 条 × n 轮,三层判定)"
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="用例 JSON 路径")
    parser.add_argument(
        "--repeat", type=int, default=3, help="每条用例重复次数(默认 3)"
    )
    parser.add_argument("--layer", default=None, help="只跑某一层")
    parser.add_argument(
        "--smoke", action="store_true", help="冒烟:只跑 golden 前 5 条"
    )
    parser.add_argument("--limit", type=int, default=None, help="最多跑多少条用例")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT), help="报告输出目录")
    parser.add_argument("--run-id", default=None, help="自定义 run_id")
    parser.add_argument(
        "--work-dir", default=None, help="临时 DB/checkpoint 目录(默认 .tmp/run_eval)"
    )
    parser.add_argument(
        "--keep-work-dir", action="store_true", help="保留本次运行的临时 DB"
    )
    parser.add_argument(
        "--no-write", action="store_true", help="只打印 JSON,不落盘报告文件"
    )
    return parser.parse_args(argv)


def _read_version(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip()
    except OSError:
        pass
    return data


def _select(cases: list[dict], args: argparse.Namespace) -> list[dict]:
    selected = cases
    if args.layer:
        selected = [c for c in selected if c.get("layer") == args.layer]
    if args.smoke:
        selected = [c for c in selected if c.get("layer") == "golden"][:5]
    if args.limit is not None:
        selected = selected[: max(0, args.limit)]
    return selected


def _judge_row(
    case: dict,
    artifact: dict,
    hard,
    judge,
) -> dict:
    hard_dict = hard.to_dict()
    verdict = judge.judge(case, artifact, hard_dict)
    judge_dict = verdict.to_dict()
    passed = bool(
        hard.task_success and not hard.critical and verdict.passed
    )
    return {
        "case_id": case["case_id"],
        "layer": case.get("layer"),
        "attempt": artifact.get("attempt"),
        "passed": passed,
        "task_success": bool(hard.task_success),
        "critical": bool(hard.critical),
        "hard": hard_dict,
        "judge": judge_dict,
        "latency_ms": float(artifact.get("latency_ms_total") or 0.0),
        "cost_usd": float((artifact.get("usage") or {}).get("cost_usd") or 0.0),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    os.chdir(ROOT)  # engine 的 seed/政策路径按 ROOT 解析

    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    errors = validate_cases(cases)
    if errors:
        print(f"[run_eval] 用例校验失败({len(errors)} 项):", file=sys.stderr)
        for error in errors[:20]:
            print(f"  - {error}", file=sys.stderr)
        return 2

    summary = stats(cases)
    selected = _select(cases, args)
    if not selected:
        print("[run_eval] 过滤后没有用例可跑", file=sys.stderr)
        return 2

    run_id = args.run_id or (
        "w4a-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + os.urandom(3).hex()
    )
    work_dir = Path(args.work_dir or (DEFAULT_TMP / run_id))
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out_dir)
    version = _read_version(ROOT / "data" / "eval" / "VERSION.txt")
    rubric_version = selected[0].get("rubric_version")
    repeat = max(1, int(args.repeat))

    print(
        f"[run_eval] run_id={run_id} cases={len(selected)}/{summary['total']} "
        f"repeat={repeat} layer={args.layer or 'all'} smoke={args.smoke}",
        file=sys.stderr,
    )

    engine = EvalEngine(
        db_path=work_dir / "eval.db",
        checkpoint_dir=work_dir / "checkpoints",
    )
    judge = build_judge()
    rows: list[dict] = []
    try:
        for index, case in enumerate(selected, start=1):
            print(
                f"[run_eval] ({index}/{len(selected)}) {case['case_id']} "
                f"[{case.get('layer')}]",
                file=sys.stderr,
            )
            for attempt in range(1, repeat + 1):
                try:
                    artifact = engine.run_case(case, attempt=attempt)
                    rows.append(_judge_row(case, artifact, hard_evaluate(case, artifact), judge))
                except Exception as exc:  # 单条用例异常不能拖垮整轮评测
                    traceback.print_exc(file=sys.stderr)
                    rows.append(
                        {
                            "case_id": case["case_id"],
                            "layer": case.get("layer"),
                            "attempt": attempt,
                            "passed": False,
                            "task_success": False,
                            "critical": False,
                            "hard": {
                                "case_id": case["case_id"],
                                "task_success": False,
                                "critical": False,
                                "failed_checks": [
                                    {
                                        "check": "engine_error",
                                        "detail": f"{type(exc).__name__}: {exc}",
                                        "severity": "major",
                                    }
                                ],
                                "failures": [],
                            },
                            "judge": None,
                            "latency_ms": 0.0,
                            "cost_usd": 0.0,
                            "failure_type": "engine_error",
                        }
                    )
    finally:
        engine.close()

    report = build_report(
        rows,
        run_id=run_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        meta={
            "cases_file": str(cases_path),
            "eval_set_version": version.get("eval_set_version"),
            "rubric_version": rubric_version,
            "repeat": repeat,
            "cases_selected": len(selected),
            "quotas": summary["quotas"],
            "llm": "FakeLLM (MOCK)",
            "judge": getattr(judge, "model_name", type(judge).__name__),
            "scored_by": getattr(judge, "scored_by", "heuristic"),
            "command": " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
        },
    )

    if not args.no_write:
        json_path, md_path = write_report(report, out_dir)
        print(f"[run_eval] 报告已写入 {json_path} / {md_path}", file=sys.stderr)
    if not args.keep_work_dir:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
