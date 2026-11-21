"""R11(2026-11-21 企业工程评审):停机优雅期必须够长,且不能无限等长连接。

核验查实:`lifespan` 退出只做「停 SLA 调度器 → stop_worker → dispose」,
**没有应用层 in-flight 排空**,依赖 uvicorn;而 compose **无 `stop_grace_period`**
(默认 10s),WS 侧 `proxy_read_timeout 3600s`。长连接在停机时会被 SIGKILL 截断。

取值**有实测依据,不是拍的**:框架侧压测(`docs/性能基线_20261121.md`)
写路径 max 达 **5612ms**(c16)。reference 判据:grace ≥ 最长请求 × 1.2 ≈ 6.7s。
本轮取 **30s**(留足余量),并给 uvicorn `--timeout-graceful-shutdown 20`
—— **必须小于 compose 的 grace**,否则 uvicorn 还在等长连接就被 SIGKILL 截断,
等于 grace 白设。

⚠️ 口径:5612ms 是**FakeLLM 框架侧**的最长请求;真模型档未测(用户裁定暂不跑),
真实最长请求可能更长,届时需按新数据重算 grace。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"

#: 压测实测的框架侧最长请求(ms),见 docs/性能基线_20261121.md 写 c16 max。
MEASURED_MAX_REQUEST_MS = 5612
#: reference 判据:grace ≥ 最长请求 × 1.2。
REQUIRED_MIN_GRACE_S = MEASURED_MAX_REQUEST_MS / 1000.0 * 1.2


def _grace_seconds() -> float | None:
    text = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"stop_grace_period:\s*(\d+)s", text)
    return float(m.group(1)) if m else None


def _uvicorn_graceful_timeout() -> float | None:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    m = re.search(r"--timeout-graceful-shutdown\s+(\d+)", text)
    return float(m.group(1)) if m else None


def test_compose_declares_stop_grace_period():
    """默认 10s 不够:必须显式声明(否则长连接被 SIGKILL 截断)。"""
    assert _grace_seconds() is not None, "compose 必须显式设 stop_grace_period"


def test_grace_covers_measured_longest_request():
    grace = _grace_seconds()
    assert grace is not None
    assert grace >= REQUIRED_MIN_GRACE_S, (
        f"grace={grace}s 小于实测最长请求×1.2={REQUIRED_MIN_GRACE_S:.1f}s"
    )


def test_uvicorn_graceful_timeout_is_set_and_below_compose_grace():
    """uvicorn 的优雅超时**必须小于** compose grace,否则 grace 白设。"""
    inner = _uvicorn_graceful_timeout()
    outer = _grace_seconds()
    assert inner is not None, "entrypoint 必须给 --timeout-graceful-shutdown,否则长连接会无限等"
    assert outer is not None
    assert inner < outer, f"uvicorn {inner}s 必须 < compose grace {outer}s"