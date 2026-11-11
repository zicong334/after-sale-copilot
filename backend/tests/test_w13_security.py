"""W13 后端安全修复的回归锁定(ISS-60 / ISS-48 / ISS-65)。

三条都是"攻击面"缺陷,故每条都写成攻击性用例:

- ISS-60:白名单式强制鉴权(旧实现只拦 ``/api/c/`` 与 ``/api/b/``,
  ``/orders``、``/tickets``、``/refunds``、``/v1/maintenance/*``、``/api/kb/*``
  在 enforce 模式下完全裸奔)→ 补无 token 攻击、跨角色攻击、跨租户读取攻击;
- ISS-48:买家 WS 未校验 session 归属(可窃听他人会话)→ 跨买家窃听攻击;
- ISS-65:重放时间戳偏差返 401 把用户踢回登录页 → 改 4xx 语义断言。

缺陷证据的处理方式(§7.3 最小化 + 可复算):用例内嵌"修复前实现"的等价
片段 ``_legacy_required_role`` / ``_legacy_replay_status``,并断言它**确实**
会放行业务路由 / 返 401 —— 即缺陷本身被固定成可执行证据;随后断言修复后
的行为正确。这样不必依赖 git 历史也能复核"修的是什么"。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app import security
from app.api import get_db
from app.graph import build as graph_build
from app.main import app
from app.models import ChatSession, Merchant, Order
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
OTHER_MERCHANT_ID = "m-w13-other"
BUYER_CUSTOMER = "seed-cust-001"
OTHER_CUSTOMER = "seed-cust-002"
DEMO_PASSWORD = "demo1234"
SESSION_ID = "w13-session-a"
OTHER_SESSION_ID = "w13-session-b"
OTHER_ORDER_ID = "w13-other-order"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


# ---------------------------------------------------------------------------
# 修复前实现的等价片段(缺陷证据,不改生产代码)
# ---------------------------------------------------------------------------


def _legacy_required_role(path: str) -> str | None:
    """W13 修复前的 ``security.required_role``(ISS-60 缺陷原样)。

    注意:这里刻意内嵌**修复前**的前缀常量(当时没有 ``/api/kb/``),
    而不是引用 ``security.ROLE_PREFIXES``(已被本次修复改过)。
    """
    legacy_role_prefixes = (("/api/c/", "buyer"), ("/api/b/", "agent"))
    if path in security.PUBLIC_PATHS:
        return None
    for prefix in security.PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return None
    if path in security.ANY_ROLE_PATHS:
        return security.ROLE_ANY
    for prefix, role in legacy_role_prefixes:
        if path.startswith(prefix):
            return role
    return None


def _legacy_replay_status(timestamp_delta: int) -> int:
    """W13 修复前的重放时间戳校验返回码(ISS-65 缺陷原样:401)。"""
    return 401


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    """seed 账号/政策 + 一个"别的租户"的订单 + 两个买家的会话。"""
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.add(
            Merchant(id=OTHER_MERCHANT_ID, name="别家店铺", platform="demo")
        )
        session.add(
            Order(
                id=OTHER_ORDER_ID,
                merchant_id=OTHER_MERCHANT_ID,
                customer_id=OTHER_CUSTOMER,
                status="created",
                total_cents=9900,
                currency="CNY",
            )
        )
        session.add(
            ChatSession(
                id=SESSION_ID,
                merchant_id=MERCHANT_ID,
                customer_id=BUYER_CUSTOMER,
                status="active",
            )
        )
        session.add(
            ChatSession(
                id=OTHER_SESSION_ID,
                merchant_id=MERCHANT_ID,
                customer_id=OTHER_CUSTOMER,
                status="active",
            )
        )
        session.commit()
    yield


@pytest.fixture()
def client(engine, seeded, tmp_dir, monkeypatch):
    """enforce 模式 + 关闭限流/防重放开关(安全开关由各用例单独打开)。"""
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_ENFORCE)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", False)
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


def token_of(client: TestClient, username: str) -> str:
    resp = client.post(
        "/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(resp) -> str:
    return resp.json().get("error", {}).get("code", "")


# ---------------------------------------------------------------------------
# ISS-60:白名单式强制鉴权
# ---------------------------------------------------------------------------


def test_legacy_required_role_let_business_routes_through():
    """缺陷证据:修复前的实现对业务与维护路由一律放行(返回 None)。"""
    for path in (
        "/orders",
        "/tickets",
        "/refunds",
        "/v1/maintenance/decay",
        "/api/kb/items",
    ):
        assert _legacy_required_role(path) is None, path


def test_required_role_is_whitelist_based():
    """修复后:除公开路径/静态资源外,业务与维护路由一律要求合法身份。"""
    assert security.required_role("/orders") == security.ROLE_ANY
    assert security.required_role("/tickets") == security.ROLE_ANY
    assert security.required_role("/refunds") == security.ROLE_ANY
    assert security.required_role("/v1/maintenance/decay") == security.ROLE_ANY
    # /api/kb/* 归 agent 角色(同时获得令牌桶限流)。
    assert security.required_role("/api/kb/items") == "agent"
    # 角色前缀不变。
    assert security.required_role("/api/c/chat") == "buyer"
    assert security.required_role("/api/b/tickets") == "agent"
    # 公开路径与前端静态资源仍放行(否则浏览器加载页面就被 401 拦死)。
    assert security.required_role("/health") is None
    assert security.required_role("/auth/login") is None
    assert security.required_role("/chat.html") is None
    assert security.required_role("/app.js") is None
    assert security.required_role("/style.css") is None


def test_business_routes_require_token(client: TestClient):
    """攻击测试:不带 token 打业务与维护路由 → 401(旧实现是 200/裸奔)。"""
    for path in ("/orders", "/tickets", "/refunds", "/api/kb/items"):
        resp = client.get(path)
        assert resp.status_code == 401, (path, resp.status_code, resp.text)
        assert error_code(resp) == "AUTH_REQUIRED", path


def test_cross_role_token_rejected_on_business_routes(client: TestClient):
    """攻击测试:买家 token 打坐席路由 / 坐席 token 打买家路由 → 403。"""
    buyer_token = token_of(client, "buyer_demo")
    agent_token = token_of(client, "agent_demo")

    resp = client.get("/api/b/tickets", headers=bearer(buyer_token))
    assert resp.status_code == 403, resp.text
    assert error_code(resp) == "FORBIDDEN"

    resp = client.get("/api/c/handoff/tickets", headers=bearer(agent_token))
    assert resp.status_code == 403, resp.text

    resp = client.get("/api/kb/items", headers=bearer(buyer_token))
    assert resp.status_code == 403, resp.text


def test_cross_tenant_read_rejected(client: TestClient):
    """攻击测试:买家 token 读别家店铺的订单 → 404,且列表不外泄。"""
    buyer_token = token_of(client, "buyer_demo")

    resp = client.get(f"/orders/{OTHER_ORDER_ID}", headers=bearer(buyer_token))
    assert resp.status_code == 404, resp.text

    resp = client.get("/orders", headers=bearer(buyer_token))
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()]
    assert OTHER_ORDER_ID not in ids, ids

    resp = client.get("/tickets", headers=bearer(buyer_token))
    assert resp.status_code == 200, resp.text


def test_static_frontend_still_served_without_token(client: TestClient):
    """回归:白名单不能把前端静态资源也拦掉(否则页面打不开)。"""
    resp = client.get("/chat.html")
    assert resp.status_code == 200, resp.text
    resp = client.get("/app.js")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# ISS-48:WS 买家会话归属校验(防跨买家窃听)
# ---------------------------------------------------------------------------


def test_buyer_ws_cannot_join_other_buyer_session(client: TestClient):
    """攻击测试:买家 B 连买家 A 的会话 → 4403 关闭(不得订阅到频道)。"""
    buyer_a = token_of(client, "buyer_demo")
    buyer_b = token_of(client, "buyer_demo2")

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/ws/buyer/{SESSION_ID}?token={buyer_b}"
        ) as ws:
            ws.receive_json()
    assert exc.value.code == 4403, exc.value.code

    # 对照:会话 A 的主人可以正常连上。
    with client.websocket_connect(f"/ws/buyer/{SESSION_ID}?token={buyer_a}") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["channels"] == [security.channel_buyer(SESSION_ID)]


def test_buyer_ws_unknown_session_rejected(client: TestClient):
    """攻击测试:连一个不存在的会话 → 4403(不再无条件订阅)。"""
    buyer_token = token_of(client, "buyer_demo")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/ws/buyer/w13-not-exist?token={buyer_token}"
        ) as ws:
            ws.receive_json()
    assert exc.value.code == 4403, exc.value.code


# ---------------------------------------------------------------------------
# ISS-65:重放时间戳偏差不再返 401(避免把用户踢回登录页)
# ---------------------------------------------------------------------------


def test_legacy_replay_status_was_401():
    """缺陷证据:修复前时间戳超窗返 401(前端见 401 即跳登录)。"""
    assert _legacy_replay_status(600) == 401


def test_replay_timestamp_out_of_window_returns_400(client: TestClient, monkeypatch):
    """修复后:时间戳偏差超窗 → 400(客户端问题,不等于未认证)。"""
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", True)
    agent_token = token_of(client, "agent_demo")
    stale = str(int(time.time()) - 600)
    resp = client.post(
        "/api/b/tickets/seed-ticket-001/claim",
        headers={
            **bearer(agent_token),
            "X-Timestamp": stale,
            "X-Nonce": "w13-nonce-stale-0001",
        },
        json={},
    )
    assert resp.status_code == 400, resp.text
    assert error_code(resp) == "REPLAY_TIMESTAMP_INVALID"


def test_replay_nonce_still_detected(client: TestClient, monkeypatch):
    """回归:nonce 重复仍返 409(重放防护本身不能削弱)。"""
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", True)
    agent_token = token_of(client, "agent_demo")
    now = str(int(time.time()))
    headers = {
        **bearer(agent_token),
        "X-Timestamp": now,
        "X-Nonce": "w13-nonce-dup-0001",
    }
    first = client.post(
        "/api/b/tickets/seed-ticket-001/claim", headers=headers, json={}
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/b/tickets/seed-ticket-001/claim", headers=headers, json={}
    )
    assert second.status_code == 409, second.text
    assert error_code(second) == "REPLAY_DETECTED"
