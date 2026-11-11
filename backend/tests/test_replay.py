"""W8 防重放测试(分工 C,3/4):写接口的 X-Timestamp / X-Nonce 守卫。

任务书 W8 的 S3 验收(`security.require_replay_guard`):

- 合法头(时间戳在 ±5 分钟窗口内 + 首次出现的 nonce)→ 正常 200;
- 同一个 (subject, nonce) 再发一次 → 409 ``REPLAY_DETECTED``;
- 时间戳超出窗口 → 401 ``REPLAY_TIMESTAMP_INVALID``;
- 缺头 / nonce 长度非法 → 400 ``REPLAY_HEADER_REQUIRED`` / ``REPLAY_NONCE_INVALID``。

被测写接口选**坐席领单** ``POST /api/b/tickets/{id}/claim``(分工 A 契约里挂
防重放依赖的五个写接口之一,链路最短、无需先造确认卡)。用它而不是旧买家入口
``/api/c/handoff/tickets/{id}/claim`` 的原因:后者在 enforce 下正在收紧为
"身份只认 token 的 service_id、不再接受请求体兜底",买家 token 本身没有
service_id;而坐席 token 自带 ``service_id=seed-agent-1`` 与
``merchant_id=seed-merchant-1``,是本契约要覆盖的正常生产路径。

数据准备:seed 后直接插一张归属 seed-merchant-1 的 ``queued`` 工单作为可领
对象(坐席侧 ``_scope_ticket`` 只校验租户 merchant,与 customer 无关)。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.main import app
from app.models import Ticket
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
AGENT_SERVICE = "seed-agent-1"
DEMO_PASSWORD = "demo1234"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

TICKET_ID = "replay-ticket-001"
CLAIM_URL = f"/api/b/tickets/{TICKET_ID}/claim"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        # 给坐席造一张本租户可领的 queued 工单(seed 的 queued 工单在并发用例间
        # 只能领一次,自建一张保证每个用例都有干净的可领对象)。
        session.add(
            Ticket(
                id=TICKET_ID,
                merchant_id=MERCHANT_ID,
                customer_id="seed-cust-001",
                order_id="seed-order-001",
                status="queued",
                claimed_by=None,
            )
        )
        session.commit()
    yield


@pytest.fixture()
def client(engine, seeded, tmp_dir, monkeypatch):
    """enforce + 打开防重放(限流关闭,避免 429 干扰重放断言)。"""
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_ENFORCE)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", True)
    security.reset_security_state()

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


def agent_headers(client: TestClient) -> dict[str, str]:
    """agent_demo 登录,拿带 service_id/merchant_id 的坐席 token。"""
    resp = client.post(
        "/auth/login", json={"username": "agent_demo", "password": DEMO_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def replay_headers(
    *, nonce: str | None = None, timestamp: int | None = None
) -> dict[str, str]:
    return {
        "X-Timestamp": str(int(time.time()) if timestamp is None else timestamp),
        "X-Nonce": nonce or uuid.uuid4().hex,
    }


def error_code(resp) -> str:
    body = resp.json()
    assert body.get("ok") is False, body
    return body["error"]["code"]


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_claim_with_valid_replay_headers_200(client: TestClient):
    """首次出现的合法 nonce + 窗口内时间戳 → 领单成功(身份取自 token)。"""
    resp = client.post(
        CLAIM_URL,
        headers={**agent_headers(client), **replay_headers()},
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["receipt"]["status"] == "claimed"
    assert body["receipt"]["claimed_by"] == AGENT_SERVICE


def test_replayed_nonce_409(client: TestClient):
    """同一 nonce 重放(即使时间戳不同)→ 409 REPLAY_DETECTED。"""
    headers = agent_headers(client)
    nonce = uuid.uuid4().hex

    first = client.post(
        CLAIM_URL,
        headers={**headers, **replay_headers(nonce=nonce)},
        json={},
    )
    assert first.status_code == 200, first.text

    # 第二次请求已无可领工单,但防重放依赖先于路由执行,必须仍是 409。
    second = client.post(
        CLAIM_URL,
        headers={**headers, **replay_headers(nonce=nonce)},
        json={},
    )
    assert second.status_code == 409, second.text
    assert error_code(second) == "REPLAY_DETECTED"


def test_stale_timestamp_400(client: TestClient):
    """时间戳超出 ±5 分钟窗口 → 400 REPLAY_TIMESTAMP_INVALID(W13-ISS-65:时钟偏差不再返 401)。"""
    stale = int(time.time()) - (security.REPLAY_WINDOW_SECONDS + 60)
    resp = client.post(
        CLAIM_URL,
        headers={**agent_headers(client), **replay_headers(timestamp=stale)},
        json={},
    )
    assert resp.status_code == 400, resp.text
    assert error_code(resp) == "REPLAY_TIMESTAMP_INVALID"


def test_missing_replay_headers_400(client: TestClient):
    """缺 X-Timestamp / X-Nonce → 400 REPLAY_HEADER_REQUIRED。"""
    resp = client.post(CLAIM_URL, headers=agent_headers(client), json={})
    assert resp.status_code == 400, resp.text
    assert error_code(resp) == "REPLAY_HEADER_REQUIRED"


def test_short_nonce_400(client: TestClient):
    """nonce 长度低于下限 → 400 REPLAY_NONCE_INVALID。"""
    resp = client.post(
        CLAIM_URL,
        headers={
            **agent_headers(client),
            **replay_headers(nonce="short"),  # 5 < REPLAY_NONCE_MIN_LEN(8)
        },
        json={},
    )
    assert resp.status_code == 400, resp.text
    assert error_code(resp) == "REPLAY_NONCE_INVALID"
