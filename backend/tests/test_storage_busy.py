"""R16(2026-11-21 压测新发现):存储锁竞争必须是**受控降级**,不能是裸 500。

压测实测(见 docs/性能基线_20261121.md):写并发 8 时出现 HTTP 500,
根因 `chat.py:_resolve_session` 的 `INSERT INTO chat_session` 抛
`sqlite3.OperationalError: database is locked`,而全仓**没有任何 OperationalError 处理**,
落到兜底 handler 变成 500 INTERNAL_ERROR。

为什么 500 是缺陷:
- 500 会招致客户端与网关重试,在**已经饱和的写路径**上放大流量(参考判据:过载禁止返 5xx);
- 正确形态是 **503 + Retry-After + retryable**,让调用方退避而不是猛冲;
- 迁 PG 也躲不掉 —— PG 同样有序列化失败/死锁,这层处理与存储无关。

⚠️ **刻意不做"整请求自动重试"**:reference 明写「非幂等操作禁止自动重试」。
对话 POST 可能已有工具自行 commit,重放会双写。所以只做受控降级 + 可观测,
重试留给**有幂等键的调用方**。这是对评审报告初稿"有界重试"措辞的更正。

口径:锁竞争的**真实复现**依赖时序,在单进程测试里构造不稳定;
所以本文件用**确定性注入**测处理逻辑,真实并发证据由压测驱动复跑提供。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.errors import is_storage_busy, register_error_handlers


def _op_error(msg: str) -> OperationalError:
    return OperationalError("INSERT INTO chat_session ...", {}, Exception(msg))


# --- 分类:哪些算"存储忙" -------------------------------------------------

@pytest.mark.parametrize("msg", [
    "database is locked",
    "database table is locked",
    "database is busy",
    "could not serialize access due to concurrent update",   # PG 序列化失败
    "deadlock detected",                                      # PG 死锁
])
def test_lock_and_serialization_errors_are_storage_busy(msg):
    """SQLite 锁 + PG 序列化/死锁都属"稍后可重试",必须识别出来。"""
    assert is_storage_busy(_op_error(msg)) is True


@pytest.mark.parametrize("msg", [
    "no such table: chat_session",
    "no such column: order.delivered_at",
    "disk I/O error",
])
def test_schema_and_io_errors_are_not_storage_busy(msg):
    """schema 错/磁盘错**不是"稍后重试就好"**,不能伪装成 503 让调用方白重试。"""
    assert is_storage_busy(_op_error(msg)) is False


def test_non_operational_error_is_not_storage_busy():
    assert is_storage_busy(ValueError("database is locked")) is False


# --- 处理:响应形态 -------------------------------------------------------

@pytest.fixture()
def client():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/busy")
    def _busy():
        raise _op_error("database is locked")

    @app.get("/broken")
    def _broken():
        raise _op_error("no such table: chat_session")

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_lock_error_returns_503_not_500(client):
    r = client.get("/busy")
    assert r.status_code == 503, f"锁竞争必须 503,实际 {r.status_code}"
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "STORAGE_BUSY"
    assert body["error"]["retryable"] is True


def test_lock_error_carries_retry_after_header(client):
    """必须给 Retry-After,否则调用方不知道退避多久,只会猛冲。"""
    r = client.get("/busy")
    assert "retry-after" in {k.lower() for k in r.headers}
    assert int(r.headers["retry-after"]) >= 1


def test_schema_error_stays_500_not_disguised_as_busy(client):
    """schema 错仍走 500 —— 把 bug 伪装成"稍后重试"会掩盖真问题。"""
    r = client.get("/broken")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"


def test_busy_response_does_not_leak_sql_or_stack(client):
    """错误体不得泄露 SQL 语句或堆栈(与既有兜底 handler 同口径)。"""
    text = client.get("/busy").text
    assert "INSERT INTO" not in text
    assert "chat_session" not in text
    assert "Traceback" not in text