"""R13(2026-11-21 企业工程评审):前端↔后端**响应契约**测试。

原先 `test_frontend_polish.py` 的断言是**源码字符串匹配**(`'xxx' in APP_JS`)——
它只能证明"前端代码里有这个字符串",**不能证明后端真的返回了那个字段**。
后端改字段名时,字符串断言照样绿,前端静默坏掉。

本文件改成**真实响应契约**:调真实端点,断言前端实际消费的字段存在且类型正确。
并且**双向防漂移**:每个声明的字段还必须在 `frontend/app.js` / `agent.js` 里真的被读到,
否则说明契约已过期(前端不再读它了),测试会提示清理。

⚠️ 边界:这是**单体内部的前后端契约**,不是跨服务 CDC/Pact。
本项目单体单服务,无跨服务契约,故评审里把 CDC 判据显式跳过、降级为本项 P2。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import security
from app.api import get_db
from app.main import app
from scripts.seed import seed
from scripts.load_policies import parse_policies, upsert_policies

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
AGENT_JS = (ROOT / "frontend" / "agent.js").read_text(encoding="utf-8")
POLICY_DIR = ROOT / "data" / "policies"


@pytest.fixture()
def client(engine, tmp_dir, monkeypatch):
    from app.graph import build as graph_build

    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_ENFORCE)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", False)
    security.reset_security_state()
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", tmp_dir / "ckpt.db")
    with Session(engine) as s:
        seed(s)
        upsert_policies(s, parse_policies(POLICY_DIR))
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


def _assert_consumed(fields, source: str, where: str):
    """双向防漂移:声明的契约字段必须真的被前端读到。"""
    for name in fields:
        assert f".{name}" in source, f"契约字段 {name} 已不在 {where} 中被消费,契约过期需清理"


def test_chat_response_contract(client):
    """`/api/c/chat` 的顶层字段契约(app.js 实际消费的那些)。"""
    fields = ["ok", "reply", "session_id", "session_status", "trace_id", "citations"]
    _assert_consumed(fields, APP_JS, "app.js")
    r = client.post("/api/c/chat", headers=_bearer(client, "buyer_demo"),
                    json={"text": "退货运费谁承担"})
    assert r.status_code == 200, r.text
    body = r.json()
    for name in fields:
        assert name in body, f"chat 响应缺契约字段 {name}"
    assert isinstance(body["ok"], bool)
    assert isinstance(body["reply"], str)
    assert isinstance(body["session_id"], str)
    assert isinstance(body["citations"], list)


def test_citation_item_contract(client):
    """引用条目契约:C1 之后前端读 title 而非 clause_id,字段必须都在。"""
    fields = ["index", "clause_id", "title", "version", "snippet"]
    for name in fields:
        assert f"cite.{name}" in APP_JS, f"app.js 不再读 cite.{name},契约过期"
    r = client.post("/api/c/chat", headers=_bearer(client, "buyer_demo"),
                    json={"text": "退货运费谁承担"})
    cites = r.json()["citations"]
    assert cites, "政策问答必须带引用(否则本契约没被真正校验)"
    for cite in cites:
        for name in fields:
            assert name in cite, f"citation 缺契约字段 {name}"
        assert isinstance(cite["index"], int)
        assert isinstance(cite["title"], str) and cite["title"], "title 不得为空(C1)"
        assert isinstance(cite["snippet"], str)


def test_buyer_history_item_contract(client):
    """买家历史消息条目契约:前端按 is_internal 跳过渲染,字段必须存在。"""
    fields = ["content", "is_internal", "author_type"]
    _assert_consumed(fields, APP_JS, "app.js")
    h = _bearer(client, "buyer_demo")
    chat = client.post("/api/c/chat", headers=h, json={"text": "退货运费谁承担"}).json()
    r = client.get(f"/api/c/sessions/{chat['session_id']}/messages", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items
    for item in items:
        for name in fields:
            assert name in item, f"历史消息缺契约字段 {name}"
        assert isinstance(item["is_internal"], bool)


def test_suggest_response_contract(client, engine):
    """坐席建议响应契约:agent.js 读 suggested_reply 填入回复框。"""
    from app.models import Ticket, TicketMessage

    tid = "tk-r13-contract"
    with Session(engine) as s:
        s.add(Ticket(id=tid, merchant_id="seed-merchant-1", customer_id="seed-cust-001",
                     status="claimed", claimed_by="seed-agent-1", summary="质量问题"))
        s.flush()
        s.add(TicketMessage(ticket_id=tid, author_type="user", content="衣服破了要退货"))
        s.commit()
    assert "payload.suggested_reply" in AGENT_JS, "agent.js 不再读 suggested_reply,契约过期"
    r = client.post(f"/api/b/tickets/{tid}/suggest", headers=_bearer(client, "agent_demo"))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    for name in ("summary", "suggested_reply", "citations", "prompt"):
        assert name in data, f"suggest 响应缺契约字段 {name}"
    assert isinstance(data["suggested_reply"], str) and data["suggested_reply"]
    assert isinstance(data["citations"], list)