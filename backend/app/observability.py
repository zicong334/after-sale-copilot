"""可观测性:Prometheus 文本格式指标导出(零第三方依赖)。

设计口径(全部基于已落库数据,可复算;README §7.5 有同口径说明):

- **节点延迟分位**:``tool_events`` 里同一条 trace 内相邻事件的时间差,归因到
  后一条事件的 ``tool_name`` 桶。桶名即 ``tool_events.tool_name``:工具调用是
  工具名,编排事件是事件名(如 ``router__classify``)。该值是**推导近似**,
  含节点之间的编排开销,不是节点自身纯耗时(仓库当前没有节点级耗时埋点)。
- **token 分账**:
  - agent 侧来自 ``token_usage`` 表(买家会话的累计输入/输出 token);
  - judge 侧来自最近一份评测报告的 ``meta.tokens_by_role``;mock 模式的报告
    不含该字段,因此默认没有 judge 序列(不臆造 0 值以外的任何数);
  - tier 维度只以路由事件计数体现(``router_failover`` / ``llm_degraded``),
    不按 token 做无据拆分。
- **护栏拦截**:来自 ``graph__prompt_probe_block`` 事件的 ``payload.flags``,
  以及 ``handoff__create_ticket`` 事件里 ``args.reason`` 的风险枚举。
  护栏命中才落库,所以这些计数是"命中并落库"的下界。
- **工具错误率**:``tool_events`` 中 ``ok=False`` 的行,按 ``error_code`` 聚合;
  ``payload`` 含 ``event`` 键的是编排事件,不计入工具错误率。
- **熔断器状态**:``tool_events`` 中 ``router_failover`` 事件 ``payload.breaker_state``
  的最新观测值;某 tier 从未产生过 failover 事件时不输出该 tier 序列。
- **队列深度**:``job_queue`` 按 ``status`` 计数。
- **缓存命中率**:进程内 ``app.cache.semantic_cache`` 的 ``stats()``。

采集在后台线程按 ``REFRESH_INTERVAL_SECONDS`` 刷新缓存,请求路径只读缓存文本,
不在请求路径上做全表扫描。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal, utc_now
from app.models.auth import TokenUsage
from app.models.jobs import Job
from app.tools.events import ToolEvent

#: 后台刷新周期(秒)。任务书要求 5s。
REFRESH_INTERVAL_SECONDS = 5.0
#: 节点延迟只统计最近多少分钟的事件。
EVENT_WINDOW_MINUTES = 15
#: 单次采集最多读取多少行事件(防全表扫描)。
EVENT_ROW_LIMIT = 5000
#: 评测报告目录(judge 侧 token 来源)。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPORTS_DIR = _REPO_ROOT / "data" / "eval" / "reports"
#: 视为"本机"的客户端 host。``testclient`` 是进程内 TestClient,不经网络栈。
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

_RISKY_HANDOFF_REASONS = {"security_risk", "complaint"}


# ---------------------------------------------------------------------------
# 文本渲染小工具
# ---------------------------------------------------------------------------


def _escape(value: str) -> str:
    """Prometheus 标签值转义。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**pairs: Any) -> str:
    """渲染标签集合,值为 None 的键直接丢弃。"""
    parts = [
        f'{key}="{_escape(str(value))}"'
        for key, value in pairs.items()
        if value is not None
    ]
    return "{" + ",".join(parts) + "}" if parts else ""


def _num(value: float | int) -> str:
    """Prometheus 数值格式:整数不带小数点,浮点保留 6 位有效小数。"""
    if isinstance(value, bool):  # pragma: no cover - 防御
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return repr(round(float(value), 6))


def _percentile(values: list[float], quantile: float) -> float | None:
    """最近秩法分位数。样本为空返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-quantile * len(ordered) // 1))))
    return ordered[rank - 1]


def _as_utc(value: datetime | None) -> datetime | None:
    """数据库里的时间列在 SQLite 下是无时区的 UTC,统一补上 UTC 标记。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_payload(raw: str | None) -> dict[str, Any]:
    """解析事件 payload;坏数据按空字典处理(不因一行脏数据丢整批指标)。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def _load_events(session: Session) -> list[tuple[Any, ...]]:
    """读取窗口内的事件行(trace_id, step, tool_name, ok, error_code, payload, ts)。"""
    since = utc_now() - timedelta(minutes=EVENT_WINDOW_MINUTES)
    stmt = (
        select(
            ToolEvent.trace_id,
            ToolEvent.step,
            ToolEvent.tool_name,
            ToolEvent.ok,
            ToolEvent.error_code,
            ToolEvent.payload,
            ToolEvent.ts,
        )
        .where(ToolEvent.ts >= since)
        .order_by(ToolEvent.ts.asc())
        .limit(EVENT_ROW_LIMIT)
    )
    return list(session.execute(stmt).all())


def _node_latency(rows: list[tuple[Any, ...]]) -> dict[str, list[float]]:
    """按 trace 内相邻事件时间差推导节点延迟桶。"""
    by_trace: dict[str, list[tuple[int, float, str]]] = {}
    for trace_id, step, tool_name, _ok, _error_code, _payload, ts in rows:
        moment = _as_utc(ts)
        if moment is None:
            continue
        by_trace.setdefault(str(trace_id or ""), []).append(
            (int(step or 0), moment.timestamp(), str(tool_name or "unknown"))
        )

    buckets: dict[str, list[float]] = {}
    for entries in by_trace.values():
        entries.sort(key=lambda item: (item[0], item[1]))
        for previous, current in zip(entries, entries[1:]):
            delta_ms = (current[1] - previous[1]) * 1000.0
            if delta_ms < 0:  # pragma: no cover - 时钟回拨兜底
                continue
            buckets.setdefault(current[2], []).append(delta_ms)
    return buckets


def _guardrail_counts(rows: list[tuple[Any, ...]]) -> dict[str, int]:
    """护栏拦截分布(prompt_probe / injection / pii / risky)。"""
    counts = {"prompt_probe": 0, "injection": 0, "pii": 0, "risky": 0}
    for _trace_id, _step, tool_name, _ok, _error_code, payload_raw, _ts in rows:
        payload = _parse_payload(payload_raw)
        event = str(payload.get("event") or "")
        if event == "graph__prompt_probe_block":
            counts["prompt_probe"] += 1
            flags = payload.get("flags")
            if isinstance(flags, list):
                flag_text = {str(item) for item in flags}
                if flag_text & {"injection_suspect", "injection_block"}:
                    counts["injection"] += 1
                if "privacy_block" in flag_text:
                    counts["pii"] += 1
        if str(tool_name or "") == "handoff__create_ticket":
            args = payload.get("args")
            if isinstance(args, dict):
                reason = str(args.get("reason") or "")
                if reason in _RISKY_HANDOFF_REASONS:
                    counts["risky"] += 1
    return counts


def _tool_counts(
    rows: list[tuple[Any, ...]],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """返回 (每工具 ok 计数, 每工具 error 计数, 每工具 error_code 计数)。"""
    ok_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    code_counts: dict[str, int] = {}
    for _trace_id, _step, tool_name, ok, error_code, payload_raw, _ts in rows:
        payload = _parse_payload(payload_raw)
        if "event" in payload:
            continue  # 编排事件不是工具调用,不计入工具错误率
        name = str(tool_name or "unknown")
        if ok:
            ok_counts[name] = ok_counts.get(name, 0) + 1
            continue
        error_counts[name] = error_counts.get(name, 0) + 1
        code = str(error_code or "UNKNOWN")
        key = f"{name}|{code}"
        code_counts[key] = code_counts.get(key, 0) + 1
    return ok_counts, error_counts, code_counts


def _route_counts(rows: list[tuple[Any, ...]]) -> tuple[dict[str, int], dict[str, str]]:
    """返回 (tier 事件计数, 各 tier 最新熔断状态)。"""
    tier_counts: dict[str, int] = {}
    breaker_state: dict[str, str] = {}
    for _trace_id, _step, tool_name, _ok, _error_code, payload_raw, _ts in rows:
        payload = _parse_payload(payload_raw)
        event = str(payload.get("event") or "")
        if event == "router_failover":
            to_tier = str(payload.get("to") or "unknown")
            tier_counts[to_tier] = tier_counts.get(to_tier, 0) + 1
            state = str(payload.get("breaker_state") or "")
            if state:
                breaker_state[to_tier] = state
        elif event == "llm_degraded" or str(tool_name or "") == "llm_degraded":
            tier_counts["degraded"] = tier_counts.get("degraded", 0) + 1
    return tier_counts, breaker_state


def _agent_tokens(session: Session) -> tuple[int, int, int]:
    """agent 侧 token 合计与有记录的账号数。"""
    rows = session.execute(
        select(
            TokenUsage.subject,
            TokenUsage.tokens_in,
            TokenUsage.tokens_out,
        )
    ).all()
    tokens_in = sum(int(row[1] or 0) for row in rows)
    tokens_out = sum(int(row[2] or 0) for row in rows)
    return tokens_in, tokens_out, len(rows)


def _eval_tokens() -> dict[str, int]:
    """judge 侧 token:取最近一份评测报告的 meta.tokens_by_role(mock 报告无此字段)。"""
    if not _REPORTS_DIR.is_dir():
        return {}
    candidates = [path for path in _REPORTS_DIR.glob("*.json") if path.is_file()]
    if not candidates:
        return {}
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - 坏报告兜底
        return {}
    meta = data.get("meta") if isinstance(data, dict) else None
    if not isinstance(meta, dict):
        return {}
    by_role = meta.get("tokens_by_role")
    if not isinstance(by_role, dict):
        return {}
    result: dict[str, int] = {}
    for role, value in by_role.items():
        if isinstance(value, dict):
            for key in ("tokens_in", "tokens_out"):
                if isinstance(value.get(key), int):
                    result[f'{role}_{key}'] = int(value[key])
        elif isinstance(value, int):
            result[str(role)] = value
    return result


def _queue_depth(session: Session) -> dict[str, int]:
    """job_queue 按 status 计数。"""
    rows = session.execute(
        select(Job.status, Job.id).limit(EVENT_ROW_LIMIT)
    ).all()
    depth: dict[str, int] = {}
    for status, _job_id in rows:
        key = str(status or "unknown")
        depth[key] = depth.get(key, 0) + 1
    return depth


def _dead_oldest_age_seconds(session: Session) -> float:
    """最老死信的年龄(秒)。R6(2026-11-21 企业工程评审)。

    为什么光有 `copilot_job_queue_depth{status="dead"}` 不够:
    计数看不出"有一条已经躺了一周"。**年龄才是可行动的信号** ——
    告警应基于它(阈值属运维决策,本轮不拍数字)。
    无死信返回 0.0。
    """
    rows = session.execute(
        select(Job.created_at).where(Job.status == "dead").limit(EVENT_ROW_LIMIT)
    ).all()
    stamps = [r[0] for r in rows if r[0] is not None]
    if not stamps:
        return 0.0
    now = utc_now()
    ages = []
    for stamp in stamps:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        ages.append((now - stamp).total_seconds())
    return float(max(ages)) if ages else 0.0


def _cache_stats() -> dict[str, int | float]:
    """进程内语义缓存统计(延迟导入,避免未启用时也拉起 sklearn 依赖)。"""
    try:
        from app.cache import semantic_cache
    except Exception:  # pragma: no cover - 依赖缺失兜底
        return {}
    stats = semantic_cache.stats()
    hits = int(stats.get("hits", 0) or 0)
    misses = int(stats.get("misses", 0) or 0)
    total = hits + misses
    return {
        "entries": int(stats.get("size", 0) or 0),
        "hits": hits,
        "misses": misses,
        "hit_ratio": (hits / total) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_metrics(session: Session) -> str:
    """采集一次并渲染成 Prometheus 文本格式。"""
    lines: list[str] = []
    rows = _load_events(session)

    # 1) 节点延迟分位
    latency = _node_latency(rows)
    lines.append("# HELP copilot_node_latency_ms 节点延迟分位(毫秒),由同 trace 相邻事件时间差推导的近似值")
    lines.append("# TYPE copilot_node_latency_ms gauge")
    for node in sorted(latency):
        samples = latency[node]
        for quantile in (0.5, 0.95):
            value = _percentile(samples, quantile)
            if value is None:
                continue
            lines.append(
                "copilot_node_latency_ms"
                + _labels(node=node, quantile=quantile)
                + " "
                + _num(value)
            )
    lines.append("# HELP copilot_node_latency_samples 节点延迟桶的样本数")
    lines.append("# TYPE copilot_node_latency_samples gauge")
    for node in sorted(latency):
        lines.append(
            "copilot_node_latency_samples" + _labels(node=node) + " " + _num(len(latency[node]))
        )

    # 2) token 分账
    tokens_in, tokens_out, subjects = _agent_tokens(session)
    lines.append("# HELP copilot_agent_tokens_total 买家会话累计 token(来源 token_usage 表)")
    lines.append("# TYPE copilot_agent_tokens_total counter")
    lines.append(
        "copilot_agent_tokens_total" + _labels(direction="input") + " " + _num(tokens_in)
    )
    lines.append(
        "copilot_agent_tokens_total" + _labels(direction="output") + " " + _num(tokens_out)
    )
    lines.append("# HELP copilot_agent_token_accounts 有 token 记录的账号数")
    lines.append("# TYPE copilot_agent_token_accounts gauge")
    lines.append("copilot_agent_token_accounts " + _num(subjects))

    eval_tokens = _eval_tokens()
    lines.append("# HELP copilot_eval_tokens_total 评测消耗 token(来源最近一份评测报告 meta.tokens_by_role)")
    lines.append("# TYPE copilot_eval_tokens_total counter")
    for key in sorted(eval_tokens):
        lines.append(
            "copilot_eval_tokens_total" + _labels(series=key) + " " + _num(eval_tokens[key])
        )

    # 3) 路由 tier 分账
    tier_counts, breaker_state = _route_counts(rows)
    lines.append("# HELP copilot_llm_route_events_total LLM 路由事件计数(fallback/degraded,来源 tool_events)")
    lines.append("# TYPE copilot_llm_route_events_total counter")
    for tier in sorted(tier_counts):
        lines.append(
            "copilot_llm_route_events_total" + _labels(tier=tier) + " " + _num(tier_counts[tier])
        )
    lines.append("# HELP copilot_circuit_breaker_state 熔断器最新状态观测(closed=1,half_open=1,open=1)")
    lines.append("# TYPE copilot_circuit_breaker_state gauge")
    for tier in sorted(breaker_state):
        state = breaker_state[tier]
        for known in ("closed", "half_open", "open"):
            lines.append(
                "copilot_circuit_breaker_state"
                + _labels(tier=tier, state=known)
                + " "
                + _num(1 if state == known else 0)
            )

    # 4) 护栏拦截
    guardrail = _guardrail_counts(rows)
    lines.append("# HELP copilot_guardrail_blocks_total 护栏命中计数(仅统计已落库事件,为下界)")
    lines.append("# TYPE copilot_guardrail_blocks_total counter")
    for name in ("prompt_probe", "injection", "pii", "risky"):
        lines.append(
            "copilot_guardrail_blocks_total"
            + _labels(guardrail=name)
            + " "
            + _num(guardrail[name])
        )

    # 5) 工具错误率
    ok_counts, error_counts, code_counts = _tool_counts(rows)
    lines.append("# HELP copilot_tool_events_total 工具调用计数(按工具与结果)")
    lines.append("# TYPE copilot_tool_events_total counter")
    for name in sorted(ok_counts):
        lines.append(
            "copilot_tool_events_total" + _labels(tool=name, result="ok") + " " + _num(ok_counts[name])
        )
    for name in sorted(error_counts):
        lines.append(
            "copilot_tool_events_total"
            + _labels(tool=name, result="error")
            + " "
            + _num(error_counts[name])
        )
    lines.append("# HELP copilot_tool_errors_total 工具错误计数(按工具与错误码)")
    lines.append("# TYPE copilot_tool_errors_total counter")
    for key in sorted(code_counts):
        name, _, code = key.partition("|")
        lines.append(
            "copilot_tool_errors_total"
            + _labels(tool=name, error_code=code)
            + " "
            + _num(code_counts[key])
        )
    lines.append("# HELP copilot_tool_error_ratio 工具错误率(错误数/总调用数)")
    lines.append("# TYPE copilot_tool_error_ratio gauge")
    for name in sorted(set(ok_counts) | set(error_counts)):
        total = ok_counts.get(name, 0) + error_counts.get(name, 0)
        ratio = (error_counts.get(name, 0) / total) if total else 0.0
        lines.append("copilot_tool_error_ratio" + _labels(tool=name) + " " + _num(ratio))

    # 6) 队列深度
    depth = _queue_depth(session)
    lines.append("# HELP copilot_job_queue_depth 后台任务队列深度(按状态)")
    lines.append("# TYPE copilot_job_queue_depth gauge")
    for status in sorted(depth):
        lines.append(
            "copilot_job_queue_depth" + _labels(status=status) + " " + _num(depth[status])
        )
    # R6:最老死信年龄 —— 计数看不出"躺了一周",年龄才是可行动信号。
    lines.append(
        "# HELP copilot_job_dead_oldest_age_seconds 最老死信(dead)的存活秒数;无死信为 0"
    )
    lines.append("# TYPE copilot_job_dead_oldest_age_seconds gauge")
    lines.append(
        "copilot_job_dead_oldest_age_seconds " + _num(_dead_oldest_age_seconds(session))
    )

    # 7) 缓存命中率
    cache = _cache_stats()
    lines.append("# HELP copilot_semantic_cache_entries 语义缓存条目数")
    lines.append("# TYPE copilot_semantic_cache_entries gauge")
    lines.append("copilot_semantic_cache_entries " + _num(cache.get("entries", 0)))
    lines.append("# HELP copilot_semantic_cache_hits_total 语义缓存命中次数")
    lines.append("# TYPE copilot_semantic_cache_hits_total counter")
    lines.append("copilot_semantic_cache_hits_total " + _num(cache.get("hits", 0)))
    lines.append("# HELP copilot_semantic_cache_misses_total 语义缓存未命中次数")
    lines.append("# TYPE copilot_semantic_cache_misses_total counter")
    lines.append("copilot_semantic_cache_misses_total " + _num(cache.get("misses", 0)))
    lines.append("# HELP copilot_semantic_cache_hit_ratio 语义缓存命中率")
    lines.append("# TYPE copilot_semantic_cache_hit_ratio gauge")
    lines.append("copilot_semantic_cache_hit_ratio " + _num(cache.get("hit_ratio", 0.0)))

    lines.append("# HELP copilot_metrics_last_refresh_unixtime 最近一次采集的 Unix 时间")
    lines.append("# TYPE copilot_metrics_last_refresh_unixtime gauge")
    lines.append("copilot_metrics_last_refresh_unixtime " + _num(int(time.time())))

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 缓存与后台刷新
# ---------------------------------------------------------------------------


class MetricsCache:
    """线程安全的指标文本缓存。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text: str | None = None
        self._updated_at: float | None = None

    def get(self) -> str | None:
        with self._lock:
            return self._text

    def set(self, text: str) -> None:
        with self._lock:
            self._text = text
            self._updated_at = time.time()

    def clear(self) -> None:
        """丢弃缓存(测试用例之间隔离用;下次请求会现算一次)。"""
        with self._lock:
            self._text = None
            self._updated_at = None

    def is_stale(self, max_age_seconds: float) -> bool:
        """缓存是否已超过 ``max_age_seconds``;从未建立过也算过期。"""
        with self._lock:
            if self._text is None or self._updated_at is None:
                return True
            return (time.time() - self._updated_at) > max_age_seconds

    @property
    def updated_at(self) -> float | None:
        with self._lock:
            return self._updated_at


#: 进程级缓存实例。
CACHE = MetricsCache()


def refresh(session: Session) -> str:
    """采集一次并写入缓存。"""
    text = render_metrics(session)
    CACHE.set(text)
    return text


def start_background_refresh(
    interval: float = REFRESH_INTERVAL_SECONDS,
) -> threading.Thread | None:
    """启动后台刷新线程(daemon,进程退出即止)。已启动时返回 None。"""
    thread = getattr(start_background_refresh, "_thread", None)
    if thread is not None and thread.is_alive():
        return None

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                with SessionLocal() as session:
                    refresh(session)
            except Exception:  # pragma: no cover - 后台采集失败不影响服务
                pass

    worker = threading.Thread(
        target=_loop, name="copilot-metrics-refresh", daemon=True
    )
    worker.start()
    setattr(start_background_refresh, "_thread", worker)
    return worker


# ---------------------------------------------------------------------------
# 暴露策略
# ---------------------------------------------------------------------------


def metrics_enabled() -> bool:
    """配置开关:METRICS_ENABLED=0/false/off 时关闭。"""
    raw = os.getenv("METRICS_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def allow_remote_clients() -> bool:
    """是否允许非本机访问(默认不允许)。"""
    raw = os.getenv("METRICS_ALLOW_REMOTE", "0").strip().lower()
    return raw in {"1", "true", "on", "yes"}


def background_refresh_enabled() -> bool:
    """后台 5s 刷新线程开关(默认开;测试可置 0 避免守护线程跨用例写缓存)。"""
    raw = os.getenv("METRICS_BACKGROUND_REFRESH", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def client_is_local(client_host: str | None) -> bool:
    """判断请求来源是否为本机。"""
    if not client_host:
        return False
    return client_host.strip().lower() in _LOCAL_HOSTS
