#!/usr/bin/env python
"""run_eval.py — 一条命令跑完评测(W4-A 基线 / W4-C 真实模型)。

用法(在项目根执行):
    .venv/Scripts/python backend/scripts/run_eval.py --smoke            # Fake 冒烟
    .venv/Scripts/python backend/scripts/run_eval.py --repeat 3         # Fake 全量
    .venv/Scripts/python backend/scripts/run_eval.py --real --smoke     # 真实模型冒烟
    .venv/Scripts/python backend/scripts/run_eval.py --real --repeat 3  # 真实正式评测

流程:装载用例 → 校验(48 条/配额/17 字段)→ 逐条 × repeat 跑图
→ 三层判定(硬断言 / 裁判 / 报告汇总)→ 写 data/eval/reports/<run_id>.json+md
→ 把报告 JSON 原文打到 stdout。

模式纪律(重要)
--------------
- **默认 FakeLLM + FakeJudge**(mode=mock):pytest 的交付测试会直接调用
  ``run_eval.bat --smoke``,默认必须是零成本 MOCK,否则有 .env 的机器跑测试会烧钱。
- ``--real`` 才加载 ``ROOT/.env``(只加载,不打印),注入 RealLLMClient 与真实裁判;
  token 用量按每条用例前后的客户端快照差值统计。若单价未配置,meta 标 ``pricing=TBD``
  (token 真实、成本数值为 0 占位),绝不编造单价。
- 401/402/403/429 或 5xx → :class:`LLMBatchAbort` 立即中止,返回码 3,
  **不写正式报告**,避免批量失败继续烧钱。
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

# ---------------------------------------------------------------------------
# 评测 harness 内部模式,生产默认 enforce 不受影响。
#
# 评测引擎(EvalEngine)走进程内 runner 直连,不经 HTTP 鉴权;但评测过程会
# 装载 app.main 的服务层上下文,一旦进程环境里出现 AUTH_MODE=enforce 之类的
# 外部设定,内部直连路径会被鉴权语义污染。故在**本进程内**把 AUTH_MODE 固定
# 为 internal:内部调用链免鉴权,身份由评测用例的 customer_id/merchant_id 决定。
# 只影响本脚本进程,不写 .env、不改服务默认值——服务/生产默认仍是 enforce
# (缺 AUTH_MODE 即 enforce,见 app/security.py:auth_mode)。
# ---------------------------------------------------------------------------
os.environ.setdefault("AUTH_MODE", "internal")

from app.eval.engine import EvalEngine  # noqa: E402
from app.eval.hard_assert import evaluate as hard_evaluate  # noqa: E402
from app.eval.judge import build_judge  # noqa: E402
from app.eval.report import build_report, write_report  # noqa: E402
from app.eval.validate_cases import load_cases, stats, validate_cases  # noqa: E402
from app.llm import build_llm, has_real_key  # noqa: E402
from app.llm.real_client import (  # noqa: E402
    LLMBatchAbort,
    RealLLMClient,
    UsageStats,
    pricing_from_env,
)

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
    # ---- W4-C 真实模型开关(默认关闭,保证 pytest 零成本)----------------
    parser.add_argument(
        "--real",
        action="store_true",
        help="接真实 LLM + 真实裁判(读取 ROOT/.env;缺 key 直接失败)",
    )
    parser.add_argument(
        "--capture",
        default=None,
        help="脱敏请求/响应 JSONL 路径(仅 --real;默认 .tmp/w4c_capture/<run_id>.jsonl)",
    )
    parser.add_argument(
        "--no-capture", action="store_true", help="关闭真实模式抓包"
    )
    parser.add_argument(
        "--price-in", type=float, default=None, help="输入单价(USD/百万 token)"
    )
    parser.add_argument(
        "--price-out", type=float, default=None, help="输出单价(USD/百万 token)"
    )
    parser.add_argument(
        "--max-tokens", type=int, default=2048, help="真实模型单次输出预算(≥1024)"
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="真实模型请求超时(秒)"
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


def _load_real_env() -> None:
    """只由 --real 显式调用:加载 ROOT/.env,绝不打印任何值。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise SystemExit("[run_eval] --real 需要 ROOT/.env,但文件不存在")
    from dotenv import load_dotenv

    load_dotenv(env_path, override=False)


def _usage_delta(before: UsageStats | None, after: UsageStats | None) -> UsageStats:
    if before is None or after is None:
        return UsageStats()
    return after - before


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

    prefix = "w4c-" if args.real else "w4a-"
    run_id = args.run_id or (
        prefix
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

    if args.real:
        _load_real_env()
        if not has_real_key():
            print("[run_eval] --real 已启用但进程环境缺少 LLM_API_KEY", file=sys.stderr)
            return 2

    capture_path: Path | None = None
    if args.real and not args.no_capture:
        capture_path = (
            Path(args.capture)
            if args.capture
            else (ROOT / ".tmp" / "w4c_capture" / f"{run_id}.jsonl")
        )

    agent_llm = (
        build_llm(
            temperature=0.2,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            capture_path=capture_path,
            price_in_per_mtok=args.price_in,
            price_out_per_mtok=args.price_out,
        )
        if args.real
        else None
    )
    judge_client = (
        build_llm(
            temperature=0.1,  # 裁判固定低温(W4-A 未尽事项④)
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            capture_path=capture_path,
        )
        if args.real
        else None
    )
    if args.real and not isinstance(agent_llm, RealLLMClient):
        print("[run_eval] --real 但未能构造真实客户端(检查 .env 的 key)", file=sys.stderr)
        return 2
    judge = build_judge(judge_client) if judge_client is not None else build_judge()

    print(
        f"[run_eval] run_id={run_id} cases={len(selected)}/{summary['total']} "
        f"repeat={repeat} layer={args.layer or 'all'} smoke={args.smoke} "
        f"mode={'real' if args.real else 'mock'}",
        file=sys.stderr,
    )

    engine = EvalEngine(
        db_path=work_dir / "eval.db",
        checkpoint_dir=work_dir / "checkpoints",
        llm=agent_llm,
    )
    rows: list[dict] = []
    judge_fallback_count = 0
    abort_error: LLMBatchAbort | None = None
    try:
        for index, case in enumerate(selected, start=1):
            print(
                f"[run_eval] ({index}/{len(selected)}) {case['case_id']} "
                f"[{case.get('layer')}]",
                file=sys.stderr,
            )
            for attempt in range(1, repeat + 1):
                try:
                    agent_before = getattr(agent_llm, "usage", None)
                    artifact = engine.run_case(case, attempt=attempt)
                    agent_after = getattr(agent_llm, "usage", None)
                    hard = hard_evaluate(case, artifact)
                    judge_before = getattr(judge_client, "usage", None)
                    row = _judge_row(case, artifact, hard, judge)
                    judge_after = getattr(judge_client, "usage", None)
                    if args.real:
                        agent_usage = _usage_delta(agent_before, agent_after)
                        judge_usage = _usage_delta(judge_before, judge_after)
                        total_usage = agent_usage + judge_usage
                        row["cost_usd"] = float(
                            total_usage.cost_usd(
                                agent_llm.price_in_per_mtok,
                                agent_llm.price_out_per_mtok,
                            )
                            or 0.0
                        )
                        row["cost_status"] = (
                            "actual"
                            if total_usage.cost_usd(
                                agent_llm.price_in_per_mtok,
                                agent_llm.price_out_per_mtok,
                            )
                            is not None
                            else "TBD(未配置单价)"
                        )
                        row["tokens"] = total_usage.to_dict()
                        row["tokens_by_role"] = {
                            "agent": agent_usage.to_dict(),
                            "judge": judge_usage.to_dict(),
                        }
                        # 裁判兜底行不参与 agreement(避免 heuristic 混入 LLM 一致率)
                        if row.get("judge") and row["judge"].get("fallback"):
                            judge_fallback_count += 1
                            row["judge_fallback"] = row.pop("judge")
                            row["judge"] = None
                    rows.append(row)
                except LLMBatchAbort as exc:  # 批次级错误:立即中止,不降级
                    raise
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
    except LLMBatchAbort as exc:
        abort_error = exc
    finally:
        engine.close()
        for client in (agent_llm, judge_client):
            close = getattr(client, "close", None)
            if callable(close):
                close()

    if abort_error is not None:
        print(
            "[run_eval] 批次中止(401/402/403/429/5xx,立即中止不重试):"
            f"{abort_error}。未写报告,请检查 key/余额/限流后由负责人决定是否重跑。",
            file=sys.stderr,
        )
        return 3

    agent_final = getattr(agent_llm, "usage", None)
    judge_final = getattr(judge_client, "usage", None)
    price_in, price_out = (
        (agent_llm.price_in_per_mtok, agent_llm.price_out_per_mtok)
        if args.real
        else (None, None)
    )
    if args.real and (price_in is None or price_out is None):
        price_in, price_out = pricing_from_env()
    total_usage = _usage_delta(UsageStats(), agent_final) + _usage_delta(
        UsageStats(), judge_final
    )

    meta: dict = {
        "cases_file": str(cases_path),
        "eval_set_version": version.get("eval_set_version"),
        "rubric_version": rubric_version,
        "repeat": repeat,
        "cases_selected": len(selected),
        "quotas": summary["quotas"],
        "llm": getattr(agent_llm, "model_name", "FakeLLM (MOCK)"),
        "judge": getattr(judge, "model_name", type(judge).__name__),
        "scored_by": getattr(judge, "scored_by", "heuristic"),
        "command": " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
    }
    if args.real:
        meta.update(
            {
                "mode": "real",
                "assumptions": (
                    "真实模型 + 真实 LLM 裁判(与被执行模型同源,agreement 存在"
                    "自偏好偏差风险);latency 仅统计对话轮/确认,不含裁判耗时"
                ),
                "model_base_url": agent_llm.base_url,
                "temperatures": {
                    "agent": agent_llm.temperature,
                    "judge": getattr(judge_client, "temperature", 0.1),
                },
                "max_tokens": args.max_tokens,
                "pricing": {
                    "input_per_mtok_usd": price_in,
                    "output_per_mtok_usd": price_out,
                    "status": "TBD" if (price_in is None or price_out is None) else "provided",
                    "note": (
                        "单价未经官方确认时成本按 0 占位;token 用量为真实 API usage,"
                        "见 meta.tokens"
                    ),
                },
                "tokens": total_usage.to_dict(),
                "tokens_by_role": {
                    "agent": (
                        _usage_delta(UsageStats(), agent_final).to_dict()
                        if agent_final
                        else {}
                    ),
                    "judge": (
                        _usage_delta(UsageStats(), judge_final).to_dict()
                        if judge_final
                        else {}
                    ),
                },
                "judge_fallback_count": judge_fallback_count,
                "judge_same_source_risk": True,
                "capture": str(capture_path) if capture_path else None,
            }
        )

    # 逐行明细写到 .tmp(供人工抽检;正式报告仍只含聚合指标)
    if args.real:
        detail_path = ROOT / ".tmp" / "w4c" / f"{run_id}.rows.jsonl"
        detail_path.parent.mkdir(parents=True, exist_ok=True)
        with detail_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + chr(10))
        print(f"[run_eval] 逐行明细 {detail_path}", file=sys.stderr)

    report = build_report(
        rows,
        run_id=run_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        meta=meta,
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
