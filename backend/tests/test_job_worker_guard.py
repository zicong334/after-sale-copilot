"""R9(2026-11-21 企业工程评审):单 worker 假设必须有**代码级护栏**,不能只写在 docstring。

背景:B2 修复引入 `reclaim_stale_running(max_age_seconds=None)`,默认**回收全部 running**。
这依赖本项目显式选择的**单机单 worker**模型。多进程/多 worker 部署时,
全量回收会**抢走别的进程正在执行的作业**。

口径当时只写进了 docstring —— **没有任何代码阻止误用**。
下一个人在多进程下起服务就会踩,而且踩了不报错(作业被静默抢走)。
本项目既有方法论就是「确定性护栏优于约定」,这里补齐。

护栏口径:
- 声明的 worker 数 > 1 且仍用**全量回收** → **拒绝启动**,并指出两条出路;
- 设了 `JOB_RECLAIM_MAX_AGE_SECONDS`(只回收超龄)→ 多 worker 下**放行**;
- 单 worker(默认)→ 放行,行为一字不变。

⚠️ 能力边界(诚实标注):本护栏只识别**显式声明**的 worker 数
(`WEB_CONCURRENCY` / `JOB_WORKER_COUNT`)。多副本 compose、外部编排起多进程
而**不设这两个变量**时,护栏识别不到 —— 那需要 DB 级 worker 租约,属更大改动,不在本轮。
"""
from __future__ import annotations

import pytest

from app import jobs


@pytest.fixture()
def queue(engine):
    q = jobs.SQLiteJobQueue(engine, poll_interval=0.01)
    yield q
    q.stop(timeout=1.0)


def test_single_worker_default_starts_fine(queue, monkeypatch):
    """正向:默认单 worker,行为一字不变(防守卫过度把正常启动拦掉)。"""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("JOB_WORKER_COUNT", raising=False)
    monkeypatch.delenv("JOB_RECLAIM_MAX_AGE_SECONDS", raising=False)
    queue.start()
    assert queue.stats()["worker_running"] is True


def test_multi_worker_with_full_reclaim_is_rejected(queue, monkeypatch):
    """多 worker + 全量回收 → 拒绝启动,错误里要给出两条出路。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.delenv("JOB_RECLAIM_MAX_AGE_SECONDS", raising=False)
    with pytest.raises(RuntimeError) as ei:
        queue.start()
    msg = str(ei.value)
    assert "JOB_RECLAIM_MAX_AGE_SECONDS" in msg
    assert "worker" in msg.lower()


def test_multi_worker_with_age_based_reclaim_is_allowed(queue, monkeypatch):
    """正向:多 worker 但只回收超龄 → 放行(安全路径必须真的能走)。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("JOB_RECLAIM_MAX_AGE_SECONDS", "60")
    queue.start()
    assert queue.stats()["worker_running"] is True


def test_explicit_job_worker_count_also_triggers_guard(queue, monkeypatch):
    """显式 JOB_WORKER_COUNT 同样触发(不只看 uvicorn 的变量)。"""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setenv("JOB_WORKER_COUNT", "3")
    monkeypatch.delenv("JOB_RECLAIM_MAX_AGE_SECONDS", raising=False)
    with pytest.raises(RuntimeError):
        queue.start()


def test_worker_count_one_is_fine(queue, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.delenv("JOB_RECLAIM_MAX_AGE_SECONDS", raising=False)
    queue.start()
    assert queue.stats()["worker_running"] is True


def test_invalid_max_age_is_explicit_error_not_silent_none(queue, monkeypatch):
    """非法取值必须显式报错,**不能静默退回 None** —— 那等于悄悄恢复全量回收。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("JOB_RECLAIM_MAX_AGE_SECONDS", "not-a-number")
    with pytest.raises(RuntimeError):
        queue.start()