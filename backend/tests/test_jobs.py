"""W10-3 异步任务队列(SQLite 作业表 + 单 worker 线程)测试。

覆盖口径:
1. enqueue → worker 消费 → handler 的写入落库(异步模式);
2. handler 抛异常 → 重试 2 次后进 dead-letter(status='dead' / attempts==3 /
   last_error 非空 / 行仍在表里);
3. 无 worker 时内联执行(调用方 session 里立刻能看到 handler 的写入);
4. graceful stop(worker 退出、stats 显示停止、之后不再消费);
5. 队列按 engine 注册(不同 engine 拿到不同实例,同一 engine 拿到同一实例);
6. run_once() 手动消费一批待处理作业。

全程只用本地临时 SQLite,不发任何网络请求,不读 .env。
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import Column, MetaData, String, Table, insert, select
from sqlalchemy.orm import Session

from app import jobs
from app.db import create_db_engine, gen_id
from app.models.jobs import Job

#: 测试用的落库表(handler 往这里写东西,断言 worker 跑完后能查到)
_TEST_META = MetaData()
job_test_log = Table(
    "job_test_log",
    _TEST_META,
    Column("id", String(36), primary_key=True),
    Column("job_id", String(36), nullable=False),
    Column("note", String(200), nullable=True),
)

#: 测试用的作业类型(每个用例独立,避免全局 handler 表互相污染)
KIND_CONSUME = "test_consume"
KIND_FAIL = "test_fail"
KIND_INLINE = "test_inline"
KIND_MANUAL = "test_manual"


@pytest.fixture()
def test_log_table(engine):
    """在测试 engine 上建一张专用表(handler 的写入目标)。"""
    _TEST_META.create_all(engine)
    yield job_test_log
    try:
        _TEST_META.drop_all(engine)
    except Exception:
        # 调用方 session 仍持有读事务时 SQLite 会报 database is locked;
        # 测试库是每用例独立的临时文件,残留表不影响断言。
        pass


@pytest.fixture(autouse=True)
def _clean_handlers():
    """每个用例结束后清掉本模块注册的 handler(全局注册表,必须清理)。"""
    yield
    for kind in (KIND_CONSUME, KIND_FAIL, KIND_INLINE, KIND_MANUAL):
        jobs.unregister_handler(kind)


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """轮询等待条件成立(不长时间 sleep,避免拖慢测试)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _log_count(session: Session) -> int:
    """统计测试表行数(独立查询,避免命中 session 缓存)。"""
    return len(list(session.execute(select(job_test_log.c.id)).all()))


def _job_row(session: Session, job_id: str) -> Job | None:
    """按 id 取作业行(先 expire,保证读到最新状态)。"""
    session.expire_all()
    return session.get(Job, job_id)


def _job_status(session: Session, job_id: str) -> str | None:
    """取作业当前状态(行不存在时返回 None)。"""
    row = _job_row(session, job_id)
    return row.status if row is not None else None


# ---------------------------------------------------------------------------
# 1) 异步模式:enqueue → worker 消费 → 结果落库
# ---------------------------------------------------------------------------


def test_worker_consumes_job_and_persists_handler_write(engine, session, test_log_table):
    """worker 在跑时 enqueue 返回作业 id,worker 消费后 handler 的写入落库。"""
    def handler(sess, payload):
        sess.execute(
            insert(job_test_log).values(id=gen_id(), job_id=payload["job_id"], note=payload.get("note"))
        )
        sess.commit()

    jobs.register_handler(KIND_CONSUME, handler)
    queue = jobs.get_queue(engine)
    queue.start()
    try:
        job_id = queue.enqueue(session, KIND_CONSUME, {"job_id": "J1", "note": "异步消费"})
        assert job_id is not None, "有 worker 时 enqueue 必须返回作业 id"
        # 作业行写在调用方事务里:worker 要等调用方 commit 才看得见
        session.commit()

        assert _wait_until(
            lambda: _job_status(session, job_id) == jobs.STATUS_DONE
        ), "worker 未消费作业"
        row = _job_row(session, job_id)
        assert row is not None
        assert row.status == jobs.STATUS_DONE
        assert row.attempts == 1
        assert row.finished_at is not None
        assert _log_count(session) == 1, "handler 的写入必须落库"
    finally:
        queue.stop()


# ---------------------------------------------------------------------------
# 2) 失败重试 2 次后进 dead-letter
# ---------------------------------------------------------------------------


def test_failed_job_goes_to_dead_letter_after_three_attempts(engine, session, test_log_table):
    """handler 一直抛异常 → attempts 达到 3 → status='dead',行仍留在表里。"""

    def handler(sess, payload):
        raise RuntimeError("处理器炸了")

    jobs.register_handler(KIND_FAIL, handler)
    queue = jobs.get_queue(engine)
    queue.start()
    try:
        job_id = queue.enqueue(session, KIND_FAIL, {"x": 1})
        assert job_id is not None
        session.commit()

        def _is_dead() -> bool:
            row = _job_row(session, job_id)
            return row is not None and row.status == jobs.STATUS_DEAD

        assert _wait_until(_is_dead), "作业未进入 dead-letter"
        row = _job_row(session, job_id)
        assert row is not None, "dead-letter 行必须仍在表里(不删除)"
        assert row.status == jobs.STATUS_DEAD
        assert row.attempts == 3, "最多尝试 3 次(首次 + 重试 2 次)"
        assert row.last_error, "dead-letter 必须记 last_error"
        assert "RuntimeError" in row.last_error
        assert "处理器炸了" in row.last_error
        assert row.finished_at is not None
    finally:
        queue.stop()


# ---------------------------------------------------------------------------
# 3) 无 worker 时内联执行(双模式)
# ---------------------------------------------------------------------------


def test_inline_mode_executes_in_caller_session_without_worker(engine, session, test_log_table):
    """没有 worker 时,enqueue 在调用方 session 里同步执行并返回 None。"""
    calls: list[str] = []

    def handler(sess, payload):
        calls.append(payload.get("tag", ""))
        sess.execute(
            insert(job_test_log).values(id=gen_id(), job_id=payload["job_id"], note="内联执行")
        )
        sess.commit()

    jobs.register_handler(KIND_INLINE, handler)
    queue = jobs.get_queue(engine)
    assert queue.stats()["worker_running"] is False

    result = queue.enqueue(session, KIND_INLINE, {"job_id": "J2", "tag": "inline"})

    assert result is None, "内联模式没有异步作业 id"
    assert calls == ["inline"], "handler 必须被同步调用"
    # 调用方 session 里立刻能查到 handler 的写入(不需要 worker)
    assert _log_count(session) == 1
    # 内联模式不写作业行
    assert session.query(Job).count() == 0


def test_inline_mode_missing_handler_raises(engine, session):
    """内联模式下没有注册 handler 必须显式失败(不能静默吞掉)。"""
    queue = jobs.get_queue(engine)
    with pytest.raises(jobs.JobHandlerNotFound):
        queue.enqueue(session, "test_no_such_kind", {})


# ---------------------------------------------------------------------------
# 4) graceful stop
# ---------------------------------------------------------------------------


def test_stop_is_graceful_and_worker_stops_consuming(engine, session, test_log_table):
    """stop() 后 worker 线程退出、stats 显示停止,之后 enqueue 走内联模式。"""
    def handler(sess, payload):
        sess.execute(
            insert(job_test_log).values(id=gen_id(), job_id=payload["job_id"], note="stop 前")
        )
        sess.commit()

    jobs.register_handler(KIND_INLINE, handler)
    queue = jobs.get_queue(engine)
    queue.start()
    assert queue.stats()["worker_running"] is True
    assert queue.stats()["mode"] == "async"

    queue.stop(timeout=5.0)

    stats = queue.stats()
    assert stats["worker_running"] is False
    assert stats["worker_alive"] is False
    assert stats["mode"] == "inline"

    # 线程不再消费:stop 之后入队走内联(返回 None),而不是异步写行
    result = queue.enqueue(session, KIND_INLINE, {"job_id": "J3"})
    assert result is None
    assert _log_count(session) == 1


def test_stop_is_idempotent(engine, session):
    """重复 stop 不报错(应用关停路径常见)。"""
    queue = jobs.get_queue(engine)
    queue.start()
    queue.stop()
    queue.stop()
    assert queue.stats()["worker_running"] is False


# ---------------------------------------------------------------------------
# 5) 队列按 engine 注册
# ---------------------------------------------------------------------------


def test_queue_is_registered_per_engine(tmp_dir):
    """不同 engine 拿到不同队列实例,同一 engine 拿到同一实例。"""
    eng_a = create_db_engine(f"sqlite:///{(tmp_dir / 'a.db').as_posix()}")
    eng_b = create_db_engine(f"sqlite:///{(tmp_dir / 'b.db').as_posix()}")
    try:
        queue_a = jobs.get_queue(eng_a)
        queue_b = jobs.get_queue(eng_b)
        assert queue_a is not queue_b
        assert jobs.get_queue(eng_a) is queue_a
        assert jobs.get_queue(eng_b) is queue_b
    finally:
        eng_a.dispose()
        eng_b.dispose()


def test_enqueue_helper_uses_session_bind(engine, session, test_log_table):
    """模块级 enqueue() 按 session.get_bind() 找到对应队列。"""
    def handler(sess, payload):
        sess.execute(
            insert(job_test_log).values(id=gen_id(), job_id=payload["job_id"], note="便捷函数")
        )
        sess.commit()

    jobs.register_handler(KIND_INLINE, handler)
    result = jobs.enqueue(session, KIND_INLINE, {"job_id": "J4"})
    assert result is None
    assert _log_count(session) == 1


# ---------------------------------------------------------------------------
# 6) run_once() 手动消费一批
# ---------------------------------------------------------------------------


def test_run_once_processes_pending_job(engine, session, test_log_table):
    """run_once() 手动消费一条 pending 作业,返回处理条数。"""
    def handler(sess, payload):
        sess.execute(
            insert(job_test_log).values(id=gen_id(), job_id=payload["job_id"], note="手动消费")
        )
        sess.commit()

    jobs.register_handler(KIND_MANUAL, handler)
    queue = jobs.get_queue(engine)

    job = Job(
        kind=KIND_MANUAL,
        payload='{"job_id": "J5"}',
        status=jobs.STATUS_PENDING,
        attempts=0,
        max_attempts=3,
    )
    session.add(job)
    session.commit()

    processed = queue.run_once()
    assert processed == 1
    assert _log_count(session) == 1
    row = _job_row(session, job.id)
    assert row is not None
    assert row.status == jobs.STATUS_DONE
    assert row.attempts == 1


def test_run_once_does_not_loop_on_failing_job(engine, session):
    """失败的作业回到 pending,但单次 run_once 内不会对它反复重试。"""
    calls: list[int] = []

    def handler(sess, payload):
        calls.append(1)
        raise RuntimeError("总是失败")

    jobs.register_handler(KIND_MANUAL, handler)
    queue = jobs.get_queue(engine)
    job = Job(
        kind=KIND_MANUAL,
        payload="{}",
        status=jobs.STATUS_PENDING,
        attempts=0,
        max_attempts=3,
    )
    session.add(job)
    session.commit()

    processed = queue.run_once()
    assert processed == 1
    assert len(calls) == 1, "单次 run_once 内同一作业最多尝试一次"
    row = _job_row(session, job.id)
    assert row is not None
    assert row.status == jobs.STATUS_PENDING
    assert row.attempts == 1
    assert row.last_error and "RuntimeError" in row.last_error


# ---------------------------------------------------------------------------
# 7) 统计
# ---------------------------------------------------------------------------


def test_stats_reports_worker_state_and_counts(engine, session):
    """stats() 给出 worker 状态与各状态作业计数。"""
    queue = jobs.get_queue(engine)
    stats = queue.stats()
    for key in ("mode", "worker_running", "worker_alive", "pending", "running", "done", "dead"):
        assert key in stats

    session.add(
        Job(
            kind=KIND_MANUAL,
            payload="{}",
            status=jobs.STATUS_PENDING,
            attempts=0,
            max_attempts=3,
        )
    )
    session.commit()
    stats = queue.stats()
    assert stats["pending"] == 1
    assert stats["done"] == 0


# ---------------------------------------------------------------------------
# B2(2026-11-17 审计):崩溃后残留 running 作业必须被回收
# ---------------------------------------------------------------------------

KIND_RECLAIM = "test_reclaim"


def _pending_job(session: Session, *, kind: str, attempts: int = 0,
                 max_attempts: int = 3) -> str:
    """直接建一条 pending 作业行(enqueue 在无 worker 时会退化为内联执行)。"""
    jid = gen_id()
    session.add(Job(id=jid, kind=kind, payload='{"tag":"reclaim"}',
                    status="pending", attempts=attempts, max_attempts=max_attempts))
    session.commit()
    return jid


def test_start_reclaims_orphaned_running_job(engine, session, test_log_table):
    """进程崩溃留下的 running 作业,重启后必须被回收并重新消费。

    缺陷:STATUS_RUNNING 全仓只被写入(抢占时)、从未被查询回收,
    无租约/心跳/启动清理。崩溃或重启会让作业永久停在 running——
    既不重试也不进 dead-letter,而异步队列是生产必需路径且每轮对话入队。
    """
    done: list[str] = []

    def handler(sess, payload):
        done.append(payload.get("tag", "?"))
        sess.commit()

    jobs.register_handler(KIND_RECLAIM, handler)
    try:
        jid = _pending_job(session, kind=KIND_RECLAIM)
        q1 = jobs.SQLiteJobQueue(engine)
        claimed = q1._claim_next(set())          # 抢占成 running
        assert claimed is not None and claimed.id == jid
        with Session(engine) as s:
            assert s.get(Job, jid).status == "running"
        # 模拟崩溃:丢弃 q1,handler 从未执行
        del q1
        assert done == []

        # 模拟重启:新队列实例 start() 应回收残留
        q2 = jobs.SQLiteJobQueue(engine)
        try:
            q2.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with Session(engine) as s:
                    if s.get(Job, jid).status == "done":
                        break
                time.sleep(0.02)
        finally:
            q2.stop()
        with Session(engine) as s:
            job = s.get(Job, jid)
            assert job.status == "done", f"残留作业未被回收消费,status={job.status}"
        assert done == ["reclaim"], "handler 应被重新执行恰好一次"
    finally:
        jobs.unregister_handler(KIND_RECLAIM)


def test_reclaim_sends_exhausted_running_job_to_dead_letter(engine, session):
    """重试已耗尽的残留 running 作业,回收时直接进 dead-letter,不无限重试。"""
    jid = _pending_job(session, kind=KIND_RECLAIM, attempts=2, max_attempts=3)
    q1 = jobs.SQLiteJobQueue(engine)
    claimed = q1._claim_next(set())              # attempts 变 3 = 已耗尽
    assert claimed is not None and claimed.attempts == 3
    del q1

    q2 = jobs.SQLiteJobQueue(engine)
    reclaimed = q2.reclaim_stale_running()
    assert reclaimed == 1
    with Session(engine) as s:
        job = s.get(Job, jid)
        assert job.status == "dead", f"耗尽的残留应进 dead-letter,实际 {job.status}"
        assert job.finished_at is not None
