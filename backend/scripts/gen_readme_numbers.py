#!/usr/bin/env python
"""gen_readme_numbers.py — 从测试收集数与评测报告 JSON 重算 README 数字。

README 里的测试/评测数字不允许手写(验收教训:README 101 vs 75)。本脚本:

1. 跑 ``pytest --collect-only -q`` 取测试用例数(不执行用例,秒级);
2. 读 ``data/eval/cases_v1.json`` 统计评测集规模与分层配额;
3. 在 ``data/eval/reports/*.json`` 中选**全量 FakeLLM 基线**报告
   (``meta.mode == "mock"``、``repeat == 3``、``cases_selected == 用例总数``,
   多条时取 ``generated_at`` 最新),重算通过率/一致率/p95/成本;
4. 输出带 ``<!-- gen_readme_numbers:begin/end -->`` 标记的 markdown 片段,
   README 原样粘贴,``backend/tests/test_delivery.py`` 逐字比对。

用法(在项目根执行)::

    .venv/Scripts/python backend/scripts/gen_readme_numbers.py
    .venv/Scripts/python backend/scripts/gen_readme_numbers.py --report data/eval/reports/<run_id>.json

本脚本**不执行** 48×3 评测,也不读取项目密钥文件或任何 API key;数字全部来自盘上资产。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
CASES_PATH = ROOT / "data" / "eval" / "cases_v1.json"
VERSION_PATH = ROOT / "data" / "eval" / "VERSION.txt"
REPORTS_DIR = ROOT / "data" / "eval" / "reports"
TMP_DIR = ROOT / ".tmp"

BEGIN_MARK = "<!-- gen_readme_numbers:begin -->"
END_MARK = "<!-- gen_readme_numbers:end -->"

_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")


def _pick_python() -> str:
    """优先使用项目 .venv 解释器(与 README/测试命令一致)。"""
    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def collect_test_count() -> int:
    """跑 pytest --collect-only 并解析 "N tests collected"。"""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    log_path = TMP_DIR / f"gen_readme_collect_{os.getpid()}.log"
    cmd = [
        _pick_python(),
        "-m",
        "pytest",
        "backend/tests",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{BACKEND}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env["TMP"] = str(TMP_DIR)
    env["TEMP"] = str(TMP_DIR)
    try:
        with log_path.open("wb") as log:
            proc = subprocess.run(
                cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=180
            )
        text = log_path.read_text(encoding="utf-8", errors="replace")
    finally:
        log_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        tail = "\n".join(text.strip().splitlines()[-15:])
        raise SystemExit(f"[gen_readme_numbers] pytest --collect-only 失败:\n{tail}")
    match = _COLLECTED_RE.search(text)
    if not match:
        raise SystemExit(
            "[gen_readme_numbers] 无法从 pytest 输出解析用例数:\n"
            + "\n".join(text.strip().splitlines()[-10:])
        )
    return int(match.group(1))


def cases_stats() -> tuple[int, dict[str, int]]:
    """读评测用例文件,返回 (总数, 各层条数)。"""
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data if isinstance(data, list) else data.get("cases", [])
    layers = Counter(str(case.get("layer")) for case in cases)
    return len(cases), dict(sorted(layers.items()))


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_full_mock_report(report: dict, total_cases: int) -> bool:
    meta = report.get("meta") or {}
    if meta.get("mode") != "mock":
        return False
    if int(meta.get("repeat") or 0) != 3:
        return False
    if int(meta.get("cases_selected") or 0) != total_cases:
        return False
    return "metrics" in report


def pick_report(total_cases: int, override: str | None = None) -> Path:
    """选全量 FakeLLM 基线报告;override 时直接校验后使用。"""
    if override:
        path = Path(override)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise SystemExit(f"[gen_readme_numbers] 报告不存在:{path}")
        return path
    candidates: list[tuple[str, Path]] = []
    for path in REPORTS_DIR.glob("*.json"):
        try:
            report = _load_json(path)
        except (OSError, ValueError):
            continue
        if _is_full_mock_report(report, total_cases):
            candidates.append((str(report.get("generated_at") or ""), path))
    if not candidates:
        raise SystemExit(
            f"[gen_readme_numbers] 未找到全量 FakeLLM 基线报告"
            f"(mode=mock / repeat=3 / cases_selected={total_cases})"
        )
    return max(candidates)[1]


def _fmt_pct(value: float, digits: int = 4) -> str:
    return f"{float(value):.{digits}f}"


def build_fragment(
    test_count: int,
    total_cases: int,
    layer_counts: dict[str, int],
    report_path: Path,
    report: dict,
) -> str:
    meta = report.get("meta") or {}
    metrics = report.get("metrics") or {}
    pass_rate = metrics.get("pass_rate") or {}
    agreement = metrics.get("judge_agreement") or {}
    p95 = metrics.get("p95_latency_ms") or {}
    cost = metrics.get("cost_per_case_usd") or {}
    by_layer = metrics.get("by_layer") or {}
    edge = by_layer.get("edge") or {}
    attempts = int(pass_rate.get("denominator") or pass_rate.get("n") or 0)
    passed = int(pass_rate.get("passed") or 0)
    repeat = int(meta.get("repeat") or 0)
    quotas = " / ".join(f"{name} {count}" for name, count in layer_counts.items())
    rel_report = report_path.relative_to(ROOT).as_posix()
    edge_text = ""
    if edge:
        edge_text = (
            f"(edge 层 {edge.get('passed_attempts')}/{edge.get('attempts')}"
            f" = {_fmt_pct(edge.get('pass_rate', 0))})"
        )
    lines = [
        BEGIN_MARK,
        "<!-- 本段由 `python backend/scripts/gen_readme_numbers.py` 自动生成,勿手改;",
        "     数字来源:pytest --collect-only 收集数 + data/eval/reports/ 全量 FakeLLM 基线报告。 -->",
        f"- 测试:{test_count} 个 pytest 用例(收集数;全量运行命令见「快速开始」)",
        f"- 评测集:{total_cases} 条 × n={repeat} = {attempts} 次尝试"
        f"({quotas})",
        f"- FakeLLM 基线通过率:{passed}/{attempts} = {_fmt_pct(pass_rate.get('value', 0))}"
        f"{edge_text}",
        f"- critical 失败:{int(metrics.get('critical_fail_count') or 0)}",
        f"- 裁判一致率:{int(agreement.get('agreed') or 0)}/{int(agreement.get('judged') or 0)}"
        f" = {_fmt_pct(agreement.get('value', 0))}",
        f"- p95 用户等待:{float(p95.get('value') or 0):.1f} ms",
        f"- 单条成本(估算):${float(cost.get('mean') or 0):.8f};"
        f"整轮合计 ${float(cost.get('total') or 0):.7f}",
        f"- 基线报告:`{rel_report}`",
        END_MARK,
    ]
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="重算 README 测试/评测数字并输出 markdown 片段"
    )
    parser.add_argument(
        "--report",
        default=None,
        help="指定评测报告 JSON(默认自动选全量 FakeLLM 基线)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    total_cases, layer_counts = cases_stats()
    report_path = pick_report(total_cases, args.report)
    report = _load_json(report_path)
    test_count = collect_test_count()

    fragment = build_fragment(test_count, total_cases, layer_counts, report_path, report)
    print(fragment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
