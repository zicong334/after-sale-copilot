"""W11-3 工单 SLA 超时升级。

规则:``queued`` 工单超过阈值(默认 30 分钟)仍未领取 → 置 ``ticket.sla_breached``
为 True,并写一条 ``sla__alert`` trace 事件;坐席领取(claim)后该标记清零。

- ``scan_breaches(session, now=...)`` 是纯函数式扫描,测试用假时钟直接调它;
- ``start_scheduler(engine)`` 是 serve 启动的后台守护线程,间隔可配置;
- 环境变量:``SLA_QUEUED_MINUTES``(默认 30)、``SLA_SCAN_INTERVAL_SECONDS``
  (默认 60)、``SLA_SCHEDULER_ENABLED``(默认 1,测试可置 0)。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db import utc_now
from app.models import Ticket
from app.trace import record_event

logger = logging.getLogger("app.sla")

#: queued 工单超时阈值(分钟)。
SLA_QUEUED_MINUTES_DEFAULT = 30
#: 后台扫描间隔(秒)。
SLA_SCAN_INTERVAL_SECONDS_DEFAULT = 60.0
#: SLA 告警 trace 事件名。
EVENT_SLA_ALERT = "sla__alert"


def queued_threshold_minutes() -> int:
    """读取(可被测试 monkeypatch 的)超时阈值。"""
    try:
        return int(os.getenv("SLA_QUEUED_MINUTES", str(SLA_QUEUED_MINUTES_DEFAULT)))
    except ValueError:  # pragma: no cover - 非法环境变量兜底
        return SLA_QUEUED_MINUTES_DEFAULT


def scan_interval_seconds() -> float:
    """读取后台扫描间隔(秒)。"""
    try:
        return float(
            os.getenv(
                "SLA_SCAN_INTERVAL_SECONDS", str(SLA_SCAN_INTERVAL_SECONDS_DEFAULT)
            )
        )
    except ValueError:  # pragma: no cover - 非法环境变量兜底
        return SLA_SCAN_INTERVAL_SECONDS_DEFAULT


def scheduler_enabled() -> bool:
    """后台线程开关(测试默认关,serve 默认开)。"""
    return os.getenv("SLA_SCHEDULER_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _aware(value: datetime) -> datetime:
    """SQLite 取出的 datetime 可能无时区,统一按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def scan_breaches(
    session: Session,
    *,
    now: datetime | None = None,
    threshold_minutes: int | None = None,
) -> list[Ticket]:
    """扫描并标记超时未领取工单,返回本次新标记的工单列表。

    守卫式更新(``where status='queued' and sla_breached=false``)保证并发/重复
    扫描不会重复告警;每条新告警写一条 ``sla__alert`` trace 事件。
    """
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    minutes = (
        queued_threshold_minutes()
        if threshold_minutes is None
        else int(threshold_minutes)
    )
    cutoff = current - timedelta(minutes=minutes)

    candidates = list(
        session.scalars(
            select(Ticket).where(
                Ticket.status == "queued",
                Ticket.sla_breached.is_(False),
                Ticket.created_at <= cutoff,
            )
        ).all()
    )
    breached: list[Ticket] = []
    for ticket in candidates:
        result = session.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket.id,
                Ticket.status == "queued",
                Ticket.sla_breached.is_(False),
            )
            .values(sla_breached=True)
        )
        if result.rowcount != 1:  # pragma: no cover - 并发下已被领取
            continue
        session.flush()
        waited_seconds = int((current - _aware(ticket.created_at)).total_seconds())
        record_event(
            session,
            trace_id=ticket.session_id or ticket.id,
            event=EVENT_SLA_ALERT,
            payload={
                "ticket_id": ticket.id,
                "merchant_id": ticket.merchant_id,
                "waited_seconds": max(waited_seconds, 0),
                "threshold_minutes": minutes,
            },
        )
        breached.append(ticket)
    if breached:
        session.commit()
    return breached


class SlaScheduler:
    """轻量后台扫描线程(守护线程,stop 时唤醒退出)。"""

    def __init__(self, engine: Engine, interval_seconds: float | None = None) -> None:
        self._engine = engine
        self._interval = (
            scan_interval_seconds()
            if interval_seconds is None
            else float(interval_seconds)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "SlaScheduler":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._loop, name="sla-scheduler", daemon=True
        )
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with Session(self._engine) as session:
                    scan_breaches(session)
            except Exception:  # pragma: no cover - 后台线程不能把进程带崩
                logger.exception("SLA 扫描失败")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def start_scheduler(engine: Engine) -> SlaScheduler | None:
    """serve 启动入口;``SLA_SCHEDULER_ENABLED=0`` 时返回 None。"""
    if not scheduler_enabled():
        return None
    return SlaScheduler(engine).start()
