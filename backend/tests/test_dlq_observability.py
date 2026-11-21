"""R6(2026-11-21 企业工程评审):死信必须可发现、可重放,且重放幂等。

核验查实:`jobs.py` 的 `status='dead'` 行**留在表里就没人管了** ——
全仓无 DLQ 消费者、无告警。命中 reference 的「无 DLQ 消费者」反模式:
问题只能靠用户投诉发现。

⚠️ 一处措辞更正:`/metrics` **已有** `copilot_job_queue_depth{status="dead"}`,
所以"无指标"不准确。真正缺的是:
- **最老死信的年龄** —— 只有计数看不出"有一条已经躺了一周";
- **告警规则** —— `deploy/prometheus.yml` 没有 `rule_files`,一条规则都没有;
- **重放入口** —— 没有任何人工兜底通道。

重放是**写操作**,必须 `require_agent`(第四轮 D 系列刚踩过越权),
且**幂等**:用 CAS `dead → pending`,同一死信重复重放第二次必须 409,
不能悄悄排两次执行。
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import jobs, security
from app.api import get_db
from app.db import utc_now
from app.main import app
from app.models.jobs import Job
from app.observability import render_metrics


def _dead_job(engine, *, age_seconds: int = 0, job_id: str | None = None) -> str:
    jid = job_id or f"job-r6-{age_seconds}"
    with Session(engine) as s:
        s.add(Job(
            id=jid, kind=jobs.JOB_TURN_AUDIT,
            payload=json.dumps({}), status=jobs.STATUS_DEAD, attempts=3,
            created_at=utc_now() - timedelta(seconds=age_seconds),
            updated_at=utc_now() - timedelta(seconds=age_seconds),
        ))
        s.commit()
    return jid


# --- 指标:最老死信年龄 ---------------------------------------------------

def test_metrics_exposes_dead_oldest_age(engine):
    with Session(engine) as s:
        text = render_metrics(s)
    assert "copilot_job_dead_oldest_age_seconds" in text


def test_dead_oldest_age_is_zero_when_no_dead_jobs(engine):
    with Session(engine) as s:
        text = render_metrics(s)
    line = next(l for l in text.splitlines()
                if l.startswith("copilot_job_dead_oldest_age_seconds"))
    assert float(line.split()[-1]) == 0.0


def test_dead_oldest_age_reflects_oldest_not_newest(engine):
    _dead_job(engine, age_seconds=60, job_id="j-new")
    _dead_job(engine, age_seconds=3600, job_id="j-old")
    with Session(engine) as s:
        text = render_metrics(s)
    line = next(l for l in text.splitlines()
                if l.startswith("copilot_job_dead_oldest_age_seconds"))
    age = float(line.split()[-1])
    assert 3500 <= age <= 3700, f"应取最老那条(~3600s),实际 {age}"


# --- 重放:CAS 幂等 ------------------------------------------------------

def test_replay_dead_job_moves_to_pending(engine):
    jid = _dead_job(engine)
    assert jobs.replay_dead_job(engine, jid) is True
    with Session(engine) as s:
        assert s.get(Job, jid).status == jobs.STATUS_PENDING


def test_replay_is_idempotent_second_call_refused(engine):
    """同一死信重复重放必须被拒 —— 否则会排两次执行。"""
    jid = _dead_job(engine)
    assert jobs.replay_dead_job(engine, jid) is True
    assert jobs.replay_dead_job(engine, jid) is False


def test_replay_non_dead_job_refused(engine):
    jid = _dead_job(engine)
    with Session(engine) as s:
        s.get(Job, jid).status = jobs.STATUS_DONE
        s.commit()
    assert jobs.replay_dead_job(engine, jid) is False


def test_replay_unknown_job_refused(engine):
    assert jobs.replay_dead_job(engine, "no-such-job") is False


# --- 重放端点:必须 agent-only -------------------------------------------

@pytest.fixture()
def client(engine, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_ENFORCE)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", False)
    security.reset_security_state()
    with Session(engine) as s:
        from scripts.seed import seed
        seed(s)
        s.commit()

    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _bearer(client, username):
    r = client.post("/auth/login", json={"username": username, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_replay_endpoint_rejects_buyer(client, engine):
    jid = _dead_job(engine, job_id="j-authz")
    r = client.post(f"/api/b/jobs/{jid}/replay", headers=_bearer(client, "buyer_demo"))
    assert r.status_code == 403, r.text


def test_replay_endpoint_agent_ok_then_conflict(client, engine):
    jid = _dead_job(engine, job_id="j-agent")
    h = _bearer(client, "agent_demo")
    first = client.post(f"/api/b/jobs/{jid}/replay", headers=h)
    assert first.status_code == 200, first.text
    second = client.post(f"/api/b/jobs/{jid}/replay", headers=h)
    assert second.status_code == 409, second.text


def test_replay_endpoint_unknown_job_404(client, engine):
    r = client.post("/api/b/jobs/nope/replay", headers=_bearer(client, "agent_demo"))
    assert r.status_code == 404, r.text