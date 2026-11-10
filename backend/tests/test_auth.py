"""W8 接入安全测试(分工 C,1/4):JWT 鉴权边界 + 身份只从 token 来。

任务书 W8 的 S1 验收:enforce 模式下

- 无 token → 401(``AUTH_REQUIRED``),带 ``WWW-Authenticate: Bearer``;
- 角色不符(buyer 打 ``/api/b/*``、agent 打 ``/api/c/*``)→ 403 ``FORBIDDEN``;
- 过期 token → 401 ``AUTH_EXPIRED``;签名被篡改 → 401 ``AUTH_INVALID``;
- 登录失败(错口令 / 账号不存在)→ 401 ``AUTH_INVALID``,登录成功返回可用 token;
- **身份只从 token 来**:buyer_demo 在 chat 请求体里塞别人的 customer_id,
  落库会话的归属仍是 token 里的 ``seed-cust-001``(``effective_identity`` 在
  enforce 下恒等返回 token 身份)。

注意:本文件把 ``AUTH_MODE`` 固定为 enforce(生产默认),而 conftest 的
``w8_security_defaults`` autouse fixture 提供的是 internal 兜底;这里在
``client`` fixture 里覆盖,保证测的是生产行为。限流/防重放在本文件关闭,
只测鉴权与身份,避免把 401/403 的断言和 429 混在一起。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.graph import build as graph_build
from app.main import app
from app.models import ChatSession
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
BUYER_CUSTOMER = "seed-cust-001"  # buyer_demo 的 token 归属
OTHER_CUSTOMER = "seed-cust-002"  # buyer_demo2,用于"body 里冒充别人"
DEMO_PASSWORD = "demo1234"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    """真实 seed(账号 + 30 单 + 8 工单)+ 真实政策库。"""
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()
    yield


@pytest.fixture()
def client(engine, seeded, tmp_dir, monkeypatch):
    """enforce 模式的 TestClient(get_db 覆盖到临时 SQLite)。"""
    # autouse fixture 已设 internal;这里显式覆盖成生产默认的 enforce。
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_ENFORCE)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", False)
    security.reset_security_state()
    # 断点库落临时目录,别写项目 data/checkpoints.db。
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", tmp_dir / "checkpoints.db")

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def login(client: TestClient, username: str, password: str = DEMO_PASSWORD):
    """登录并返回响应(失败场景由用例自行断言)。"""
    return client.post("/auth/login", json={"username": username, "password": password})


def token_of(client: TestClient, username: str) -> str:
    """登录成功并取 token;失败直接让用例炸掉(附带响应体便于定位)。"""
    resp = login(client, username)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(resp) -> str:
    """统一错误体 ``{ok:false, error:{code,...}}`` → code。"""
    body = resp.json()
    assert body.get("ok") is False, body
    return body["error"]["code"]


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_missing_token_401(client: TestClient):
    """无 Authorization → 401 AUTH_REQUIRED,并带 Bearer 质询头。"""
    resp = client.get("/api/c/handoff/tickets")
    assert resp.status_code == 401, resp.text
    assert error_code(resp) == "AUTH_REQUIRED"
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_wrong_role_403(client: TestClient):
    """buyer token 打 /api/b/* → 403;agent token 打 /api/c/* → 403。"""
    buyer_token = token_of(client, "buyer_demo")
    resp = client.get("/api/b/tickets", headers=bearer(buyer_token))
    assert resp.status_code == 403, resp.text
    assert error_code(resp) == "FORBIDDEN"

    agent_token = token_of(client, "agent_demo")
    resp = client.get("/api/c/handoff/tickets", headers=bearer(agent_token))
    assert resp.status_code == 403, resp.text
    assert error_code(resp) == "FORBIDDEN"


def test_expired_token_401(client: TestClient):
    """ttl_seconds=-1 签出的 token 已过期 → 401 AUTH_EXPIRED。"""
    expired = security.create_token(
        security.Identity(
            sub="seed-user-buyer_demo",
            role="buyer",
            merchant_id=MERCHANT_ID,
            customer_id=BUYER_CUSTOMER,
        ),
        ttl_seconds=-1,
    )
    resp = client.get("/api/c/handoff/tickets", headers=bearer(expired))
    assert resp.status_code == 401, resp.text
    assert error_code(resp) == "AUTH_EXPIRED"
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_tampered_signature_401(client: TestClient):
    """改掉签名段首字符 → 验签失败 401 AUTH_INVALID(不得被当成合法身份)。"""
    token = token_of(client, "buyer_demo")
    head, payload, signature = token.split(".")
    flipped = "A" if signature[0] != "A" else "B"
    tampered = f"{head}.{payload}.{flipped}{signature[1:]}"

    resp = client.get("/api/c/handoff/tickets", headers=bearer(tampered))
    assert resp.status_code == 401, resp.text
    assert error_code(resp) == "AUTH_INVALID"


def test_login_failure_401(client: TestClient):
    """错口令 / 账号不存在都必须是 401 AUTH_INVALID(不泄露账号是否存在)。"""
    resp = login(client, "buyer_demo", "wrong-password")
    assert resp.status_code == 401, resp.text
    assert error_code(resp) == "AUTH_INVALID"

    resp = login(client, "no_such_user", DEMO_PASSWORD)
    assert resp.status_code == 401, resp.text
    assert error_code(resp) == "AUTH_INVALID"


def test_login_success_returns_token(client: TestClient):
    """登录成功返回可用的 Bearer token,/auth/me 能读回 token 身份。"""
    resp = login(client, "buyer_demo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body["token"]
    assert token
    assert body["token_type"].lower() == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["role"] == "buyer"
    assert body["user"]["customer_id"] == BUYER_CUSTOMER

    me = client.get("/auth/me", headers=bearer(token))
    assert me.status_code == 200, me.text
    user = me.json()["user"]
    assert user["id"] == "seed-user-buyer_demo"
    assert user["role"] == "buyer"
    assert user["customer_id"] == BUYER_CUSTOMER


def test_body_customer_id_has_no_effect(client: TestClient, engine):
    """enforce 下 body 里的 customer_id/merchant_id 没有身份效力。

    buyer_demo 登录后在 chat body 里冒充 buyer_demo2(seed-cust-002)、
    并尝试指定别的 merchant,断言落库会话仍归属 token 里的 seed-cust-001 /
    seed-merchant-1。这是 S1「身份从 token 来」的核心回归。
    """
    token = token_of(client, "buyer_demo")
    resp = client.post(
        "/api/c/chat",
        headers=bearer(token),
        json={
            "merchant_id": MERCHANT_ID,
            "customer_id": OTHER_CUSTOMER,
            "text": "你好,帮我查一下我的订单",
        },
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]

    with Session(engine) as session:
        row = session.get(ChatSession, session_id)
        assert row is not None
        assert row.customer_id == BUYER_CUSTOMER, f"body 里的 customer_id 不应生效:{row.customer_id}"
        assert row.merchant_id == MERCHANT_ID
