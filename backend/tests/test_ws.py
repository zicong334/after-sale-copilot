"""W8 WebSocket 实时通讯测试(分工 C,4/4)。

任务书 W8 的 S4 验收:TestClient 双端 WebSocket

- buyer 连 ``/ws/buyer/{session_id}?token=``:先收 ``ready``(带订阅频道),
  坐席回复后收 ``agent_message``;
- agent 连 ``/ws/agent/{service_id}?token=``:先收 ``ready``(agent + queue 两频道),
  领单后收 ``ticket_update``;
- 错角色(拿 buyer token 连 agent 端点)→ 关闭码 4403;缺 token → 4401;
- 断线降级:WS 关闭后旧轮询接口(会话摘要)仍返回 200;
- 兼容边界:``AUTH_MODE=internal`` 时 chat 不带 token 仍 200(check_w1 回归)。

时序要点:``main._serve_websocket`` 先发 ``ready`` 再 ``subscribe`` 频道。客户端
收到 ready 后立刻触发写接口,存在"事件发布了但订阅还没挂上"的毫秒级竞态,
因此每个用例 ready 之后都先发一次 ``ping``、收到 ``pong`` 再触发写操作 ——
pong 证明接收循环已启动(subscribe 已完成),事件不会丢。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app import security
from app.api import get_db
from app.graph import build as graph_build
from app.main import app
from app.models import ChatSession, Ticket
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
BUYER_CUSTOMER = "seed-cust-001"
AGENT_SERVICE = "seed-agent-1"
DEMO_PASSWORD = "demo1234"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

SESSION_ID = "ws-session-001"
TICKET_ID = "ws-ticket-001"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    """seed 账号/政策 + 一张"已转人工、已被坐席领取、带 session_id"的工单。

    seed 的 8 张工单都没有 session_id,而 agent_message/ticket_status 事件要按
    session_id 发到 buyer 频道,所以这里显式造一条完整链路。
    """
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.add(
            ChatSession(
                id=SESSION_ID,
                merchant_id=MERCHANT_ID,
                customer_id=BUYER_CUSTOMER,
                status="handed_off",
            )
        )
        session.add(
            Ticket(
                id=TICKET_ID,
                merchant_id=MERCHANT_ID,
                customer_id=BUYER_CUSTOMER,
                order_id="seed-order-001",
                session_id=SESSION_ID,
                status="claimed",
                claimed_by=AGENT_SERVICE,
            )
        )
        session.commit()
    yield


@pytest.fixture()
def client(engine, seeded, tmp_dir, monkeypatch):
    """enforce + 关闭限流/防重放(WS 测试只测实时通讯,不叠加安全开关)。"""
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


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def token_of(client: TestClient, username: str) -> str:
    resp = client.post(
        "/auth/login", json={"username": username, "password": DEMO_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def ping(ws) -> None:
    """心跳:收到 pong 说明服务端订阅已挂好、接收循环已在跑。"""
    ws.send_json({"type": "ping"})
    pong = ws.receive_json()
    assert pong["type"] == "pong", pong


def receive_event(ws, event_type: str, *, tries: int = 5) -> dict:
    """按类型取事件(跳过排队期间可能先到的其他事件)。"""
    for _ in range(tries):
        event = ws.receive_json()
        if event.get("type") == event_type:
            return event
    raise AssertionError(f"未收到 {event_type} 事件")


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_buyer_ws_ready_and_agent_message(client: TestClient):
    """买家通道:ready(订阅 buyer:<sid>)→ 坐席回复后收到 agent_message。"""
    buyer_token = token_of(client, "buyer_demo")
    agent_token = token_of(client, "agent_demo")

    with client.websocket_connect(
        f"/ws/buyer/{SESSION_ID}?token={buyer_token}"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["role"] == "buyer"
        assert ready["channels"] == [security.channel_buyer(SESSION_ID)]
        ping(ws)

        resp = client.post(
            f"/api/b/tickets/{TICKET_ID}/messages",
            headers=bearer(agent_token),
            json={"content": "您好,我是人工客服,已接手您的问题"},
        )
        assert resp.status_code == 200, resp.text

        event = receive_event(ws, "agent_message")
        assert event["ticket_id"] == TICKET_ID
        assert event["session_id"] == SESSION_ID
        assert event["author_type"] == "agent"
        assert "人工客服" in event["content"]
        assert event["message_id"] == resp.json()["message"]["id"]

    # S4 断线降级:WS 关闭后,旧轮询接口仍能拿到会话摘要(200)。
    summary = client.get(
        f"/api/c/handoff/sessions/{SESSION_ID}/summary",
        headers=bearer(buyer_token),
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["session_id"] == SESSION_ID


def test_agent_ws_ready_and_ticket_update(client: TestClient):
    """坐席通道:ready(agent + queue 频道)→ 领单后收到 ticket_update。"""
    agent_token = token_of(client, "agent_demo")

    with client.websocket_connect(
        f"/ws/agent/{AGENT_SERVICE}?token={agent_token}"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["role"] == "agent"
        assert ready["channels"] == [
            security.channel_agent(AGENT_SERVICE),
            security.channel_queue(MERCHANT_ID),
        ]
        ping(ws)

        # seed-ticket-001 是 queued 工单,领单会向 queue:<merchant> 广播 ticket_update。
        resp = client.post(
            "/api/b/tickets/seed-ticket-001/claim",
            headers=bearer(agent_token),
            json={},
        )
        assert resp.status_code == 200, resp.text

        event = receive_event(ws, "ticket_update")
        assert event["ticket_id"] == "seed-ticket-001"
        assert event["status"] == "claimed"
        assert event["claimed_by"] == AGENT_SERVICE


def test_wrong_role_ws_closed_4403(client: TestClient):
    """buyer token 连 agent 端点 → 服务端以 4403 关闭(角色不符)。"""
    buyer_token = token_of(client, "buyer_demo")
    with client.websocket_connect(
        f"/ws/agent/{AGENT_SERVICE}?token={buyer_token}"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4403


def test_missing_token_ws_closed_4401(client: TestClient):
    """不带 token 连 buyer 端点 → 4401(未认证)。"""
    with client.websocket_connect(f"/ws/buyer/{SESSION_ID}") as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4401


def test_internal_mode_chat_without_token_200(client: TestClient, monkeypatch):
    """AUTH_MODE=internal:chat 不带 Authorization 仍 200(check_w1 回归兼容)。"""
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_INTERNAL)

    resp = client.post(
        "/api/c/chat",
        json={
            "merchant_id": MERCHANT_ID,
            "customer_id": BUYER_CUSTOMER,
            "text": "你好",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["session_id"]
