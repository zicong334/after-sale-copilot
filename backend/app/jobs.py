"""jobs.py — W10-3 异步任务队列(SQLite 作业表 + 单 worker 线程)。

本模块只提供**队列基础设施**,不含任何业务逻辑:作业处理器(handler)由外部
通过 :func:`register_handler` 注册(本模块不 import 业务模块,避免与
``app.graph.nodes`` / ``app.api.chat`` 形成循环依赖)。

双模式(关键设计)
----------------
同一个 ``SQLiteJobQueue`` 有两种执行路径,取决于**worker 线程是否在跑**:

1. **异步模式**(队列有 worker 线程):
   ``enqueue()`` 只在**调用方事务**里写一行 ``job_queue``(status='pending'),
   不提交——调用方 commit 之后 worker 才看得见这行(与调用方业务写入同一事务,
   语义上"要么一起成功,要么一起回滚")。
   worker 用**自己的 session / 自己的事务**执行作业。

2. **内联模式**(队列没有 worker 线程):
   ``enqueue()`` 直接在**调用方 session / 事务**里同步执行 handler,
   语义与改造前的同步路径完全一致(冻结基线的既有测试因此不受影响)。
   ``enqueue()`` 返回 ``None``(没有作业行,也没有异步 id)。

worker 行为
-----------
- 后台线程轮询(默认间隔 0.05 秒,可配置),取一条 ``status='pending'`` 的作业并
  **抢占**(``UPDATE ... WHERE id=? AND status='pending'``),避免重复消费。
- 成功 → ``status='done'`` + ``finished_at``。
- 失败 → ``attempts`` 已 +1;``attempts >= max_attempts``(默认 3,即失败重试 2 次)
  → ``status='dead'``(dead-letter 行,**行仍留在表里**)并记 ``last_error``;
  否则回 ``status='pending'`` 等下次重试。
- ``stop()`` 是 graceful 的:置停止事件 → 等线程收尾(超时只告警,不强杀)。
- handler 抛异常不会让 worker 线程死掉(异常被捕获并转成作业状态)。
"""
from __future__ import annotations

import json
import logging
import threading
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import timezone
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db import utc_now
from app.models.jobs import Job, TurnAudit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 作业类型常量(lead 在 nodes.py / chat.py 按这些值注册 handler 与入队)
# ---------------------------------------------------------------------------

#: 记忆抽取作业(原同步路径:``app.memory.store.extract_and_write``)
JOB_MEMORY_EXTRACT = "memory_extract"
#: 工单摘要作业(原同步路径:写 ``ticket.summary``)
JOB_TICKET_SUMMARY = "ticket_summary"
#: 审计落盘作业(原同步路径:写 ``turn_audit`` 表)
JOB_TURN_AUDIT = "turn_audit"

#: 已知作业类型清单(供 lead 自检用;表层不限制,允许扩展)
JOB_KINDS: tuple[str, ...] = (
    JOB_MEMORY_EXTRACT,
    JOB_TICKET_SUMMARY,
    JOB_TURN_AUDIT,
)

# ---------------------------------------------------------------------------
# 状态常量(与 ``job_queue.status`` 的 CHECK 约束一致)
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_DEAD = "dead"

#: 默认最大尝试次数 3 = 首次 + 重试 2 次
DEFAULT_MAX_ATTEMPTS = 3
#: worker 轮询间隔(秒)
DEFAULT_POLL_INTERVAL = 0.05
#: 单次 run_once 最多处理的作业条数
DEFAULT_BATCH_SIZE = 100
#: 单条 last_error 保留的最大长度
MAX_ERROR_LENGTH = 2000

#: handler 签名:``fn(session, payload) -> None``;抛异常即视为本次尝试失败
Handler = Callable[[Session, dict[str, Any]], None]


class JobHandlerNotFound(LookupError):
    """作业类型没有注册 handler。"""


# ---------------------------------------------------------------------------
# 进程级 handler 注册表
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Handler] = {}
_HANDLERS_LOCK = threading.Lock()


def register_handler(kind: str, fn: Handler) -> None:
    """注册(覆盖)某作业类型的处理器。

    handler 签名 ``fn(session, payload) -> None``;``session`` 是执行作业时的事务
    会话(异步模式 = worker 自己的 session;内联模式 = 调用方 session)。
    抛异常即视为本次尝试失败,队列会按重试策略处理。
    """
    if not isinstance(kind, str) or not kind:
        raise ValueError("作业类型 kind 必须是非空字符串")
    if not callable(fn):
        raise TypeError("作业处理器必须是可调用对象")
    with _HANDLERS_LOCK:
        _HANDLERS[kind] = fn


def unregister_handler(kind: str) -> None:
    """注销某作业类型的处理器(测试与热更新用)。"""
    with _HANDLERS_LOCK:
        _HANDLERS.pop(kind, None)


def clear_handlers() -> None:
    """清空全部处理器(测试用;生产环境请用 register_handler)。"""
    with _HANDLERS_LOCK:
        _HANDLERS.clear()


def get_handler(kind: str) -> Handler | None:
    """取某作业类型的处理器;未注册返回 ``None``。"""
    with _HANDLERS_LOCK:
        return _HANDLERS.get(kind)


def registered_kinds() -> list[str]:
    """已注册的作业类型清单(按字典序)。"""
    with _HANDLERS_LOCK:
        return sorted(_HANDLERS)


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------


class JobQueue(ABC):
    """作业队列抽象接口(lead 按此契约调用)。"""

    @abstractmethod
    def enqueue(self, session: Session, kind: str, payload: dict) -> str | None:
        """入队一个作业。

        异步模式:在调用方事务里写作业行,返回作业 id(调用方 commit 后 worker
        才消费)。内联模式:在调用方 session 里同步执行作业,返回 ``None``。
        """

    @abstractmethod
    def start(self) -> None:
        """启动 worker 线程(幂等:已在跑则直接返回)。"""

    @abstractmethod
    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止 worker 线程:通知退出并等待收尾;超时只告警,不强制终止。"""

    @abstractmethod
    def run_once(self) -> int:
        """处理一批待处理作业,返回处理条数(测试用;worker 线程内部也用它)。"""

    @abstractmethod
    def stats(self) -> dict:
        """返回队列运行统计(worker 状态、各状态作业计数、已注册作业类型等)。"""


# ---------------------------------------------------------------------------
# SQLite 实现
# ---------------------------------------------------------------------------


class _ClaimedJob:
    """已被 worker 抢占的作业(内存态快照,避免跨 session 复用 ORM 对象)。"""

    __slots__ = ("id", "kind", "payload", "max_attempts", "attempts")

    def __init__(
        self,
        *,
        id: str,
        kind: str,
        payload: str,
        max_attempts: int,
        attempts: int,
    ) -> None:
        self.id = id
        self.kind = kind
        self.payload = payload
        self.max_attempts = max_attempts
        self.attempts = attempts


class SQLiteJobQueue(JobQueue):
    """基于 SQLite 作业表的单 worker 队列。"""

    def __init__(
        self,
        engine: Engine,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        batch_size: int = DEFAULT_BATCH_SIZE,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._engine = engine
        self._poll_interval = max(0.0, float(poll_interval))
        self._batch_size = max(1, int(batch_size))
        self._session_factory = session_factory or sessionmaker(
            bind=engine, autocommit=False, autoflush=False, expire_on_commit=False,
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # -- 对外只读属性 ------------------------------------------------------
    @property
    def engine(self) -> Engine:
        """本队列绑定的 engine。"""
        return self._engine

    @property
    def poll_interval(self) -> float:
        """worker 轮询间隔(秒)。"""
        return self._poll_interval

    # -- 入队 --------------------------------------------------------------
    def enqueue(self, session: Session, kind: str, payload: dict) -> str | None:
        """入队一个作业(见 :meth:`JobQueue.enqueue`)。"""
        if not isinstance(kind, str) or not kind:
            raise ValueError("作业类型 kind 必须是非空字符串")
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)

        if self._worker_active():
            # 异步模式:作业行写进调用方事务,与调用方业务写入同一事务。
            job = Job(
                kind=kind,
                payload=body,
                status=STATUS_PENDING,
                attempts=0,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
            )
            session.add(job)
            session.flush()  # 只在本事务内可见,不提交
            return job.id

        # 内联模式:直接在调用方 session/事务里同步执行,语义同改造前。
        handler = get_handler(kind)
        if handler is None:
            raise JobHandlerNotFound(f"作业类型 {kind!r} 未注册 handler")
        handler(session, payload or {})
        return None

    # -- worker 生命周期 ---------------------------------------------------
    def start(self) -> None:
        """启动 worker 线程(幂等);启动前先回收上次进程留下的残留 running 作业。"""
        with self._lock:
            if self._worker_active():
                return
            # B2:必须先回收再起线程,否则残留 running 永远不会被 _claim_next 看到
            # (它只 select pending)。回收失败不能挡住 worker 启动。
            try:
                self.reclaim_stale_running()
            except Exception:  # pragma: no cover - 回收失败只影响残留作业
                logger.exception("启动回收残留 running 作业失败(忽略,继续启动 worker)")
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._run_loop, name="job-worker", daemon=True,
            )
            self._thread = thread
            thread.start()

    def reclaim_stale_running(self, *, max_age_seconds: float | None = None) -> int:
        """回收残留 ``running`` 作业:重试未耗尽 → ``pending``,耗尽 → ``dead``。

        B2(2026-11-17 审计):``STATUS_RUNNING`` 原先**只被写入**(抢占时)、
        从未被任何查询回收,也没有租约/心跳。进程崩溃、被 kill、部署重启或
        worker 线程异常退出后,那条作业就**既不重试也不进 dead-letter**,永久悬空;
        而异步队列是生产必需路径(``main.lifespan``),每轮对话都入队,
        一次重启就可能让若干买家的那轮请求永远没有结果。

        口径:本项目显式选择**单机单 worker**(SQLite 单文件),所以 ``start()`` 时
        任何 ``running`` 行都是上次进程的残留,默认全部回收(``max_age_seconds=None``)。
        将来多 worker / 独立队列部署时,传 ``max_age_seconds`` 只回收超龄的,
        或改由租约(``lease_expires_at``)驱动——**不要**在多进程下用默认全量回收。

        返回回收条数。``attempts`` 保留(抢占时已 +1),因此耗尽判定与正常失败一致。
        """
        now = utc_now()
        reclaimed = 0
        with self._session_factory() as sess:
            for job in list(sess.scalars(select(Job).where(Job.status == STATUS_RUNNING))):
                if max_age_seconds is not None and job.started_at is not None:
                    started = job.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    if (now - started).total_seconds() < float(max_age_seconds):
                        continue
                attempts = int(job.attempts or 0)
                if attempts >= int(job.max_attempts or DEFAULT_MAX_ATTEMPTS):
                    job.status = STATUS_DEAD
                    job.finished_at = now
                    job.last_error = job.last_error or "进程中断且重试已耗尽(启动回收)"
                    logger.error(
                        "作业 %s(%s) 残留 running 且已尝试 %d 次,回收进 dead-letter",
                        job.id, job.kind, attempts,
                    )
                else:
                    job.status = STATUS_PENDING
                    job.started_at = None
                    logger.warning(
                        "作业 %s(%s) 残留 running(已尝试 %d 次),回收为 pending 待重试",
                        job.id, job.kind, attempts,
                    )
                job.updated_at = now
                reclaimed += 1
            sess.commit()
        return reclaimed

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止 worker 线程(超时只告警,不强制终止)。"""
        with self._lock:
            thread = self._thread
        self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning(
                    "作业 worker 未在 %.1f 秒内退出(不强制终止,线程可能仍在收尾)", timeout,
                )
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    # -- 消费 --------------------------------------------------------------
    def run_once(self) -> int:
        """处理一批待处理作业,返回处理条数。

        单次调用内每个作业**最多尝试一次**(失败的作业回到 pending,留到下次
        调用再试),避免"永远失败的作业"在一次调用里死循环。
        """
        processed = 0
        attempted: set[str] = set()
        while processed < self._batch_size:
            claimed = self._claim_next(attempted)
            if claimed is None:
                break
            attempted.add(claimed.id)
            self._execute(claimed)
            processed += 1
        return processed

    def stats(self) -> dict:
        """队列运行统计。"""
        counts = {
            STATUS_PENDING: 0,
            STATUS_RUNNING: 0,
            STATUS_DONE: 0,
            STATUS_DEAD: 0,
        }
        try:
            with self._session_factory() as sess:
                rows = sess.execute(
                    select(Job.status, func.count()).group_by(Job.status)
                ).all()
            for status, count in rows:
                counts[str(status)] = int(count)
        except Exception:  # pragma: no cover - 统计失败不应影响业务
            logger.exception("读取作业统计失败")
        active = self._worker_active()
        return {
            "mode": "async" if active else "inline",
            "worker_running": active,
            "worker_alive": self._thread is not None and self._thread.is_alive(),
            "poll_interval": self._poll_interval,
            "batch_size": self._batch_size,
            "engine_id": id(self._engine),
            "handlers": registered_kinds(),
            "pending": counts[STATUS_PENDING],
            "running": counts[STATUS_RUNNING],
            "done": counts[STATUS_DONE],
            "dead": counts[STATUS_DEAD],
            "counts": counts,
        }

    # -- 内部实现 ----------------------------------------------------------
    def _worker_active(self) -> bool:
        """worker 线程是否在运行(决定走异步还是内联模式)。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _run_loop(self) -> None:
        """worker 主循环:轮询取作业,处理,直到收到停止信号。"""
        while not self._stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception:  # pragma: no cover - 兜底:线程不能因异常退出
                logger.exception("作业 worker 循环异常(已忽略,线程继续)")
                processed = 0
            if processed == 0:
                # 没有待处理作业:等一小会儿再轮询(stop() 会立刻唤醒)
                self._stop_event.wait(self._poll_interval)

    def _claim_next(self, attempted: set[str]) -> _ClaimedJob | None:
        """抢占一条 pending 作业(原子 UPDATE,避免重复消费)。"""
        with self._session_factory() as sess:
            stmt = select(Job).where(Job.status == STATUS_PENDING)
            if attempted:
                stmt = stmt.where(Job.id.notin_(list(attempted)))
            stmt = stmt.order_by(Job.created_at.asc(), Job.id.asc()).limit(8)
            # 先物化再逐条抢占:避免在 commit 之后继续 fetch 游标
            candidates = list(sess.scalars(stmt))
            for job in candidates:
                attempts = int(job.attempts or 0) + 1
                result = sess.execute(
                    update(Job)
                    .where(Job.id == job.id, Job.status == STATUS_PENDING)
                    .values(
                        status=STATUS_RUNNING,
                        attempts=attempts,
                        started_at=utc_now(),
                        updated_at=utc_now(),
                    )
                )
                sess.commit()
                if result.rowcount == 1:
                    return _ClaimedJob(
                        id=job.id,
                        kind=job.kind,
                        payload=job.payload,
                        max_attempts=int(job.max_attempts or DEFAULT_MAX_ATTEMPTS),
                        attempts=attempts,
                    )
                # 已被别的 worker 抢走:继续看下一条
            sess.rollback()
        return None

    def _execute(self, claimed: _ClaimedJob) -> None:
        """执行一条已抢占的作业(worker 自己的 session 与事务)。"""
        try:
            payload = json.loads(claimed.payload or "{}")
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except (TypeError, ValueError):
            self._mark_failed(claimed, ValueError("作业 payload 不是合法 JSON 对象"))
            return

        handler = get_handler(claimed.kind)
        sess = self._session_factory()
        try:
            if handler is None:
                raise JobHandlerNotFound(f"作业类型 {claimed.kind!r} 未注册 handler")
            handler(sess, payload)
        except Exception as exc:  # 失败不能让 worker 线程死掉
            try:
                sess.rollback()
            except Exception:  # pragma: no cover - 回滚失败兜底
                logger.exception("作业 %s 回滚失败", claimed.id)
            self._mark_failed(claimed, exc)
            return
        finally:
            try:
                sess.close()
            except Exception:  # pragma: no cover - 关闭失败兜底
                logger.exception("作业 %s 的会话关闭失败", claimed.id)
        self._mark_done(claimed)

    def _mark_done(self, claimed: _ClaimedJob) -> None:
        """标记作业成功。"""
        with self._session_factory() as sess:
            job = sess.get(Job, claimed.id)
            if job is None:  # pragma: no cover - 行被删(不应发生)
                return
            job.status = STATUS_DONE
            job.last_error = None
            job.finished_at = utc_now()
            job.updated_at = utc_now()
            sess.commit()

    def _mark_failed(self, claimed: _ClaimedJob, exc: BaseException) -> None:
        """标记作业失败:重试未耗尽 → 回 pending;耗尽 → dead-letter。"""
        error_text = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
        with self._session_factory() as sess:
            job = sess.get(Job, claimed.id)
            if job is None:  # pragma: no cover - 行被删(不应发生)
                return
            job.last_error = error_text
            job.updated_at = utc_now()
            if claimed.attempts >= int(job.max_attempts or DEFAULT_MAX_ATTEMPTS):
                job.status = STATUS_DEAD
                job.finished_at = utc_now()
                logger.error(
                    "作业 %s(%s) 已尝试 %d 次仍失败,进入 dead-letter: %s",
                    job.id, job.kind, claimed.attempts, error_text,
                )
            else:
                job.status = STATUS_PENDING
                job.started_at = None
                logger.warning(
                    "作业 %s(%s) 第 %d 次失败,稍后重试: %s",
                    job.id, job.kind, claimed.attempts, error_text,
                )
            sess.commit()


# ---------------------------------------------------------------------------
# 队列注册表:按 engine 注册,同一 engine 同一实例
# ---------------------------------------------------------------------------

_QUEUES: weakref.WeakKeyDictionary[Engine, SQLiteJobQueue] = weakref.WeakKeyDictionary()
_QUEUES_LOCK = threading.Lock()


def get_queue(engine: Engine) -> SQLiteJobQueue:
    """按 engine 取队列实例(同一 engine 永远拿到同一实例)。

    用弱引用键:engine 被回收后,对应队列随之消失,避免测试里残留僵尸队列。
    """
    if engine is None:
        raise ValueError("engine 不能为空")
    with _QUEUES_LOCK:
        queue = _QUEUES.get(engine)
        if queue is None:
            queue = SQLiteJobQueue(engine)
            _QUEUES[engine] = queue
        return queue


def _engine_of(session: Session) -> Engine:
    """取 session 绑定的 engine(兼容 session 直接绑 Connection 的情况)。"""
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine is None:
        raise ValueError("无法确定 session 绑定的 engine")
    return engine


def enqueue(session: Session, kind: str, payload: dict) -> str | None:
    """便捷入队:按 ``session.get_bind()`` 找到对应队列后入队。

    异步模式返回作业 id,内联模式返回 ``None``。
    """
    queue = get_queue(_engine_of(session))
    return queue.enqueue(session, kind, payload)


def start_worker(engine: Engine) -> SQLiteJobQueue:
    """便捷启动:按 engine 取队列并启动 worker 线程(应用启动时调用)。"""
    queue = get_queue(engine)
    queue.start()
    return queue


def stop_worker(engine: Engine, timeout: float = 5.0) -> None:
    """便捷停止:按 engine 取队列并优雅停止 worker 线程(应用关停时调用)。"""
    get_queue(engine).stop(timeout)


# ---------------------------------------------------------------------------
# turn_audit 便捷写入(供 lead 的 turn_audit handler 调用)
# ---------------------------------------------------------------------------


def write_turn_audit(
    session: Session,
    *,
    trace_id: str,
    merchant_id: str,
    customer_id: str,
    intent: str = "",
    reply: str | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> TurnAudit:
    """写一行审计留痕(``turn_audit`` 表),返回该行。

    ``citations`` 以 JSON 文本存储;``reply`` 为本轮回复正文。
    """
    row = TurnAudit(
        trace_id=trace_id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        intent=intent or "",
        reply=reply,
        citations=json.dumps(citations or [], ensure_ascii=False, default=str),
        created_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row
