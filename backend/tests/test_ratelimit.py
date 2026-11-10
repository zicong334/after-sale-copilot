"""W8 限流测试(分工 C,2/4):用户级令牌桶 + 日 token 配额。

任务书 W8 的 S2 验收:

- 同一 token 对轻接口连发,超出桶(用户级 5 QPS / 突发 10)后返回 429
  ``RATE_LIMITED``,且 ``Retry-After`` 头存在(前端/网关据此退避);
- chat 的日 token 配额用尽后返回 429 ``TOKEN_QUOTA_EXCEEDED``。

实现要点(见 ``app/security.py``):

- ``MemoryRateLimiter`` 是进程内令牌桶,``security.reset_security_state()``
  重建桶;因此本文件在 ``client`` fixture 里显式打开 ``RATE_LIMIT_ENABLED``
  并 reset,保证每个用例从满桶开始、互不污染。
- 日配额按 ``token_usage`` 表当日聚合;这里预写一条超额用量 + 把
  ``DAILY_TOKEN_QUOTA`` patch 成小值,避免真的跑 20 万 token。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.graph import build as graph_build
from app.main import app
from app.models import ChatSession
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
BUYER_CUSTOMER = "seed-cust-001"
DEMO_PASSWORD = "demo1234"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

#: 轻接口:只做 token 解码 + 一次账号查询,不写库,适合打满令牌桶。
LIGHT_PATH = "/auth/me"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()
    yield


@pytest.fixture()
def client(engine, seeded, tmp_dir, monkeypatch):
    """enforce + 显式开启限流 + reset 令牌桶(每个用例从满桶开始)。"""
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_ENFORCE)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", False)
    security.reset_security_state()
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", tmp_dir / "checkpoints.db")

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()
    security.reset_security_state()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def token_of(client: TestClient, username: str) -> str:
    # /auth/login 是公开路径,不经过限流;这里只取 token。
    resp = client.post(
        "/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(resp) -> str:
    body = resp.json()
    assert body.get("ok") is False, body
    return body["error"]["code"]


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_burst_11th_request_429_with_retry_after(client: TestClient):
    """同 token 连打轻接口:前 10 次放行(突发 10),第 11 次 429 + Retry-After。

    为什么先手工消耗 9 个令牌:用户级桶 5 QPS/突发 10,而 TestClient 的每个
    请求都要走一次完整 ASGI 栈(本机实测 10 次已超过 200ms,会补回 1 个令牌),
    直接打 11 次会变成 flaky。这里用业务自己的 ``check_rate_limit`` 在同一
    subject 上先消耗 9 个令牌(与中间件同一套桶逻辑,不是伪造),再真发第
    10、11 次 HTTP 请求——两次请求间隔毫秒级,补不满 1 个令牌,第 11 次必然
    被中间件拒绝。断言点仍是「第 11 次 429 + Retry-After」。
    """
    token = token_of(client, "buyer_demo")
    headers = bearer(token)

    identity = security.decode_token(token)
    for _ in range(9):
        # 前 9 次消耗成功(桶里还剩 1 个令牌),为第 10 次 HTTP 留出放行额度。
        assert security.check_rate_limit(identity) is None

    assert client.get(LIGHT_PATH, headers=headers).status_code == 200  # 第 10 次

    resp = client.get(LIGHT_PATH, headers=headers)  # 第 11 次
    assert resp.status_code == 429, resp.text
    assert error_code(resp) == "RATE_LIMITED"
    retry_after = resp.headers.get("Retry-After")
    assert retry_after is not None, "429 必须带 Retry-After 头"
    assert int(retry_after) >= 1


def test_daily_token_quota_exceeded_429(client: TestClient, engine, monkeypatch):
    """日 token 配额用尽 → chat 入口 429 TOKEN_QUOTA_EXCEEDED,且不产生会话写入。"""
    token = token_of(client, "buyer_demo")

    # 预写一条超额用量(走业务自己的计量函数,保证当日聚合口径一致)。
    with Session(engine) as session:
        security.record_token_usage(session, "seed-user-buyer_demo", 1_000, 1_000)
        session.commit()
    monkeypatch.setattr(security, "DAILY_TOKEN_QUOTA", 10)

    resp = client.post(
        "/api/c/chat",
        headers=bearer(token),
        json={
            "merchant_id": MERCHANT_ID,
            "customer_id": BUYER_CUSTOMER,
            "text": "你好",
        },
    )
    assert resp.status_code == 429, resp.text
    assert error_code(resp) == "TOKEN_QUOTA_EXCEEDED"

    # 配额检查在会话创建之前:超限的那一轮不产生任何 ChatSession 写入。
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ChatSession)) == 0
