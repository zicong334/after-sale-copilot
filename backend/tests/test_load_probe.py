"""R1(2026-11-21 企业工程评审):压测驱动的统计与失败口径单测。

评审发现:本项目**零性能基准**。README 里的 p95 1334ms 来自 FakeLLM **功能评测**
(144 次尝试、单并发),不是负载测试 —— 两个完全不同的量,不能互相外推。

本文件只测驱动里的**纯函数**(分位数、失败判定、汇总),不实跑负载。
判据来自 enterprise-engineering-design/quality-testing §5:
- 报告只给平均值/中位数、缺 P95/P99/P999 → 判定报告无效;
- **错误率把 429、超时、业务错误码排除在失败之外 → 判定指标造假**;
- 并发当 QPS(固定 N 并发即宣称 N QPS,忽略 RT)→ 扩容算错。
"""
from __future__ import annotations

import pytest

from scripts.load_probe import is_failure, percentiles, summarize


# --- 分位数 ---------------------------------------------------------------

def test_percentiles_reports_tail_not_just_average():
    """必须给 P95/P99/P999;只给平均值等于报告无效。"""
    samples = [float(i) for i in range(1, 1001)]  # 1..1000 ms
    p = percentiles(samples)
    assert set(p) >= {"p50", "p95", "p99", "p999"}
    assert p["p50"] == pytest.approx(500, abs=2)
    assert p["p95"] == pytest.approx(950, abs=2)
    assert p["p99"] == pytest.approx(990, abs=2)
    assert p["p999"] == pytest.approx(999, abs=2)


def test_percentiles_long_tail_is_visible():
    """长尾必须体现出来:99 个 10ms + 1 个 2000ms,P99 不能被平均值掩盖。"""
    samples = [10.0] * 99 + [2000.0]
    p = percentiles(samples)
    assert p["p50"] == 10.0
    assert p["p99"] >= 10.0
    assert p["max"] == 2000.0


def test_percentiles_empty_is_explicit_not_zero():
    """空样本不得伪造 0(0ms 会被误读成"极快")。"""
    p = percentiles([])
    assert p["p99"] is None
    assert p["count"] == 0


# --- 失败口径 -------------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503, 408])
def test_rate_limited_and_server_errors_count_as_failure(status):
    """429 与 5xx **必须计入失败** —— 系统已在拒绝服务,不能报"零错误"。"""
    assert is_failure(status=status, error=None) is True


def test_timeout_and_network_error_count_as_failure():
    assert is_failure(status=None, error="TimeoutException") is True
    assert is_failure(status=None, error="ConnectError") is True


@pytest.mark.parametrize("status", [200, 201, 204])
def test_success_is_not_failure(status):
    assert is_failure(status=status, error=None) is False


def test_client_config_error_is_failure_but_distinct():
    """4xx(非 429/408)也是失败,但要能与容量类失败区分开(避免把配置错当容量不足)。"""
    assert is_failure(status=403, error=None) is True


# --- 汇总:并发 ≠ QPS ------------------------------------------------------

def test_throughput_is_measured_not_assumed_from_concurrency():
    """吞吐必须由「完成数 / 稳态时长」算出,不能拿并发数当 QPS。"""
    # 10 并发、每请求 100ms、稳态 1s → 约 100 req/s,而不是 10
    s = summarize(
        latencies_ms=[100.0] * 100,
        failures=0,
        concurrency=10,
        window_seconds=1.0,
    )
    assert s["throughput_rps"] == pytest.approx(100.0, rel=0.01)
    assert s["concurrency"] == 10
    assert s["throughput_rps"] != s["concurrency"]


def test_error_rate_includes_all_failures():
    s = summarize(
        latencies_ms=[50.0] * 90,
        failures=10,
        concurrency=4,
        window_seconds=1.0,
    )
    assert s["error_rate"] == pytest.approx(0.10, rel=0.01)
    assert s["total"] == 100


def test_zero_window_does_not_divide_by_zero():
    s = summarize(latencies_ms=[], failures=0, concurrency=1, window_seconds=0.0)
    assert s["throughput_rps"] is None
    assert s["error_rate"] is None