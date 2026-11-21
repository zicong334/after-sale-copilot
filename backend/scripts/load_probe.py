"""load_probe.py — 框架侧负载基线驱动(R1,2026-11-21 企业工程评审)。

为什么需要它
------------
本项目原先**零性能基准**:README 里的 p95 来自 FakeLLM **功能评测**
(48 条 × 3 次、单并发),那是"功能正确性跑一遍的耗时",**不是负载下的延迟分布**。
两个量不能互相外推。本驱动补的是后者。

口径(判据来自 enterprise-engineering-design/quality-testing §5)
--------------------------------------------------------------
- 报 **P50/P95/P99/P999 + max**,不只给平均值;
- **429 / 超时 / 5xx 一律计入失败** —— 系统已在拒绝服务时不能报"零错误";
- **吞吐由「完成数 ÷ 稳态时长」算出,不拿并发数当 QPS**;
- 有预热段且**预热样本丢弃**;
- 无目标 QPS 时**按并发阶梯找饱和点**,而不是按"目标百分比"加压
  (本项目用户明确没有目标 QPS/SLA,所以阶梯自变量是并发)。

**它测什么、不测什么**
- 测:FastAPI + SQLAlchemy + SQLite 的**框架与存储侧**容量曲线(FakeLLM,零 API 花费)。
- **不测**真实模型延迟 —— 真模型档需用户批准花钱,当前裁定暂不跑。
- 因此结论口径是「框架侧饱和点」,**不是**端到端生产容量。

用法
----
    # 先起服务(临时库、关限流以便测结构容量,见 --help 里的说明)
    .venv/Scripts/python backend/scripts/load_probe.py \
        --base-url http://127.0.0.1:8011 --scenario write \
        --concurrency 1,2,4,8 --duration 15 --warmup 3 --out .tmp/load_write.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 纯函数(有单测:backend/tests/test_load_probe.py)
# ---------------------------------------------------------------------------


def percentiles(samples: list[float]) -> dict[str, Any]:
    """最近秩法分位数。空样本返回 None 而**不是 0**(0ms 会被误读成"极快")。"""
    count = len(samples)
    if count == 0:
        return {"count": 0, "p50": None, "p95": None, "p99": None,
                "p999": None, "max": None, "mean": None}
    ordered = sorted(samples)

    def at(q: float) -> float:
        rank = max(1, math.ceil(q * count))
        return float(ordered[min(rank, count) - 1])

    return {
        "count": count,
        "p50": at(0.50),
        "p95": at(0.95),
        "p99": at(0.99),
        "p999": at(0.999),
        "max": float(ordered[-1]),
        "mean": float(statistics.fmean(ordered)),
    }


def is_failure(*, status: int | None, error: str | None) -> bool:
    """失败判定:**429 / 408 / 5xx / 超时 / 网络错误全部计入**。

    非 2xx 的 4xx(如 403)也算失败,但调用方应把它与容量类失败分开看——
    配置错导致的 403 不代表容量不足。
    """
    if error:
        return True
    if status is None:
        return True
    return not (200 <= status < 300)


def summarize(
    *, latencies_ms: list[float], failures: int,
    concurrency: int, window_seconds: float,
) -> dict[str, Any]:
    """汇总一个阶梯。

    ``latencies_ms`` 只含**成功**请求的耗时;``failures`` 是失败计数。
    总数 = 成功 + 失败;**吞吐 = 总数 ÷ 稳态时长**(失败也消耗了容量,要计入分母侧的完成数)。
    """
    total = len(latencies_ms) + int(failures)
    out: dict[str, Any] = {
        "concurrency": int(concurrency),
        "window_seconds": float(window_seconds),
        "total": total,
        "failures": int(failures),
        "latency_ms": percentiles(latencies_ms),
    }
    if window_seconds > 0 and total > 0:
        out["throughput_rps"] = total / window_seconds
        out["error_rate"] = int(failures) / total
    else:
        out["throughput_rps"] = None
        out["error_rate"] = None
    return out


# ---------------------------------------------------------------------------
# 负载驱动
# ---------------------------------------------------------------------------

WRITE_TEXT = "退货运费谁承担"          # 政策问答:每轮仍写 user/assistant 消息 + 审计 + 作业行
READ_PATH = "/tickets"                  # 只读对照:用于验证「单写者是瓶颈」的假设


async def _login(client, username: str, password: str) -> str:
    r = await client.post("/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["token"]


async def _worker(
    client, token: str, scenario: str, stop_at: float, warmup_until: float,
    sink: dict[str, Any],
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    while True:
        now = time.monotonic()
        if now >= stop_at:
            return
        started = time.monotonic()
        status: int | None = None
        error: str | None = None
        ttfb: float | None = None
        try:
            if scenario == "write":
                r = await client.post("/api/c/chat", headers=headers, json={"text": WRITE_TEXT})
                status = r.status_code
            elif scenario == "read":
                r = await client.get(READ_PATH, headers=headers)
                status = r.status_code
            elif scenario == "sse":
                async with client.stream(
                    "POST", "/api/c/chat/stream", headers=headers, json={"text": WRITE_TEXT}
                ) as r:
                    status = r.status_code
                    async for _chunk in r.aiter_bytes():
                        if ttfb is None:
                            ttfb = (time.monotonic() - started) * 1000.0
                        break
            else:
                raise ValueError(f"未知场景: {scenario}")
        except Exception as exc:  # noqa: BLE001 - 压测要把任何异常计为失败
            error = type(exc).__name__
        elapsed = (time.monotonic() - started) * 1000.0
        # 预热样本丢弃(连接池未建满、缓存未热,前段数据失真)
        if time.monotonic() < warmup_until:
            continue
        if is_failure(status=status, error=error):
            sink["failures"] += 1
            key = f"status:{status}" if status is not None else f"error:{error}"
            sink["failure_kinds"][key] = sink["failure_kinds"].get(key, 0) + 1
        else:
            sink["latencies"].append(elapsed)
            if ttfb is not None:
                sink["ttfb"].append(ttfb)


async def run_level(base_url: str, scenario: str, concurrency: int,
                    duration: float, warmup: float) -> dict[str, Any]:
    import httpx

    sink: dict[str, Any] = {"latencies": [], "ttfb": [], "failures": 0, "failure_kinds": {}}
    timeout = httpx.Timeout(60.0, connect=10.0)
    limits = httpx.Limits(max_connections=concurrency * 2 + 4)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout, limits=limits) as client:
        token = await _login(client, "buyer_demo", "demo1234")
        started = time.monotonic()
        warmup_until = started + warmup
        stop_at = warmup_until + duration
        await asyncio.gather(*[
            _worker(client, token, scenario, stop_at, warmup_until, sink)
            for _ in range(concurrency)
        ])
        window = time.monotonic() - warmup_until
    out = summarize(
        latencies_ms=sink["latencies"], failures=sink["failures"],
        concurrency=concurrency, window_seconds=window,
    )
    out["failure_kinds"] = sink["failure_kinds"]
    if sink["ttfb"]:
        out["ttfb_ms"] = percentiles(sink["ttfb"])
    return out


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="框架侧负载基线(FakeLLM,零 API 花费)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8011")
    ap.add_argument("--scenario", choices=("write", "read", "sse"), default="write")
    ap.add_argument("--concurrency", default="1,2,4,8")
    ap.add_argument("--duration", type=float, default=15.0, help="每阶稳态秒数")
    ap.add_argument("--warmup", type=float, default=3.0, help="预热秒数(样本丢弃)")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    report: dict[str, Any] = {
        "scenario": args.scenario,
        "base_url": args.base_url,
        "duration_per_level_s": args.duration,
        "warmup_s": args.warmup,
        "llm": "FakeLLM(框架侧口径,不含真实模型延迟)",
        "levels": [],
    }
    print(f"场景={args.scenario} 阶梯并发={levels} 每阶稳态={args.duration}s 预热={args.warmup}s")
    for c in levels:
        row = asyncio.run(run_level(args.base_url, args.scenario, c, args.duration, args.warmup))
        report["levels"].append(row)
        lat = row["latency_ms"]
        print(
            f"  并发 {c:>3}: rps={row['throughput_rps'] and round(row['throughput_rps'], 1)} "
            f"错误率={row['error_rate'] and round(row['error_rate'], 4)} "
            f"p50={lat['p50'] and round(lat['p50'], 1)} p99={lat['p99'] and round(lat['p99'], 1)} "
            f"max={lat['max'] and round(lat['max'], 1)} 失败 kinds={row['failure_kinds']}"
        )
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())