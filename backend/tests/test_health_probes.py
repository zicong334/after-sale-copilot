"""R7(2026-11-21 企业工程评审):readiness 与 liveness 必须分开。

评审发现:`/health` 是静态 200,不探任何依赖。
⚠️ **静态 liveness 本身是对的** —— reference 明确「liveness 探下游 = 级联故障放大器」:
DB 抖动会让全量实例重启。所以**不改 `/health`**,而是**新增 readiness**。

单机形态的取舍(重要):compose 只有一个 backend 实例,
**摘流 = 全站下线**。所以 readiness 在单机下**只用于告警与人工判断,不用于摘流**;
nginx 侧也不配基于它的主动摘流。多副本部署后才谈摘流。

判据:
- readiness 探 DB(`SELECT 1`),不健康返 **503**(不是 200 带 flag —— 探针只看状态码);
- liveness **不探下游**,DB 坏了仍 200,进程不该被重启;
- 两者都必须**免鉴权**(否则探针拿不到)。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import security
from app.main import app


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_ENFORCE)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", False)
    security.reset_security_state()
    with TestClient(app) as c:
        yield c


def test_ready_is_public_and_ok_when_db_reachable(client):
    r = client.get("/healthz/ready")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_liveness_stays_static_even_when_db_broken(client, monkeypatch):
    """DB 坏了 liveness 仍 200 —— 否则 DB 抖动会触发全量重启(级联放大器)。"""
    from app import main as main_mod

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(main_mod, "_db_reachable", _boom)
    assert client.get("/health").status_code == 200
    assert client.get("/healthz/ready").status_code == 503


def test_ready_returns_503_not_200_with_flag(client, monkeypatch):
    """探针只看状态码;返 200 带 ready:false 等于探针永远认为健康。"""
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "_db_reachable", lambda: False)
    r = client.get("/healthz/ready")
    assert r.status_code == 503
    assert r.json()["ok"] is False


def test_ready_does_not_leak_internal_error_detail(client, monkeypatch):
    """错误体不得泄露连接串/堆栈(与既有错误体口径一致)。"""
    from app import main as main_mod

    def _boom():
        raise RuntimeError("sqlite:///secret/path/dev.db unreachable")

    monkeypatch.setattr(main_mod, "_db_reachable", _boom)
    text = client.get("/healthz/ready").text
    assert "secret" not in text
    assert "sqlite:///" not in text


def test_both_probes_need_no_token(client):
    """免鉴权:探针不带 Bearer 也必须能拿到(否则 401 会被误判成不健康)。"""
    assert client.get("/health").status_code == 200
    assert client.get("/healthz/ready").status_code == 200