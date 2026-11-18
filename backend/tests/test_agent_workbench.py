"""test_agent_workbench.py — W5 客服工作台 API 专项验收(契约 .tmp/w5_contract.md §二/§四)。

覆盖(13 条):
1.  GET /api/b/services:两名 seed 坐席 + demo_no_password;
2.  GET /api/b/tickets:queued 优先 → 组内 last_msg_at DESC(NULL 最后);
3.  GET /api/b/tickets:工单结构完整 + status_label 中文 + status 过滤;
4.  POST claim:queued → claimed,verified 回执;
5.  重复 claim:409「已被他人领取」;
6.  GET /api/b/tickets/{id}:含转人工后追加的买家消息(author=user);
7.  POST messages:author=agent/author_label=人工客服,买家 summary 可见;
8.  关单后 reply 409、重复关单 409;
9.  关单后 claim 409;
10. 全链路:转人工→回复→关单→买家 summary ticket_closed/ticket_messages;
11. GET /api/b/tickets/{id} 未知工单 404;
12. reply 空白 content 400 VALIDATION_ERROR;
13. GET /agent.html 200 + agent.js + 无 CDN/构建工具。

只读 backend/app;本文件是 qa-tests 唯一写入产物。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import get_db
from app.graph import build as graph_build
from app.main import app
from app.models import Merchant, Ticket
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER = "seed-cust-001"
HANDOFF_TEXT = "我不跟你聊了,直接给我转人工客服"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


# ---------------------------------------------------------------------------
# fixtures(与 test_handoff_flow.py 一致:独立 SQLite + TestClient)
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()


@pytest.fixture()
def ckpt(tmp_dir, monkeypatch) -> Path:
    path = tmp_dir / "checkpoints.db"
    monkeypatch.setattr(graph_build, "DEFAULT_CHECKPOINT_PATH", path)
    return path


@pytest.fixture()
def client(engine, seeded, ckpt):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _add_ticket(
    engine,
    ticket_id: str,
    *,
    merchant: str,
    status: str = "queued",
    customer: str = "c-wb",
    claimed_by: str | None = None,
    handoff_reason: str | None = None,
    summary: str | None = None,
    last_msg_at: datetime | None = None,
    created_at: datetime | None = None,
) -> None:
    """直接落一张工单(绕过图,专测工作台路由;约束照 CHECK 不走后门)。"""
    if status in ("claimed", "waiting_customer") and claimed_by is None:
        claimed_by = "seed-agent-1"
    with Session(engine) as db:
        if db.get(Merchant, merchant) is None:
            db.add(Merchant(id=merchant, name="工作台测试店"))
            db.flush()
        db.add(
            Ticket(
                id=ticket_id,
                merchant_id=merchant,
                customer_id=customer,
                status=status,
                claimed_by=claimed_by,
                handoff_reason=handoff_reason,
                summary=summary,
                last_msg_at=last_msg_at,
                created_at=created_at or datetime.now(timezone.utc),
            )
        )
        db.commit()


def _chat(client: TestClient, text: str, *, session_id: str | None = None) -> dict:
    payload = {
        "merchant_id": MERCHANT_ID,
        "customer_id": CUSTOMER,
        "text": text,
        "context": None,
    }
    if session_id:
        payload["session_id"] = session_id
    resp = client.post("/api/c/chat", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _handoff_and_confirm(client: TestClient) -> tuple[str, str]:
    """聊天转人工 → 确认建单;返回 (session_id, ticket_id)。"""
    first = _chat(client, HANDOFF_TEXT)
    token = first["pending_confirm"]["confirm_token"]
    resp = client.post(
        f"/api/c/confirm/{token}/confirm", json={"thread_id": first["session_id"]}
    )
    assert resp.status_code == 200, resp.text
    return first["session_id"], resp.json()["receipt"]["ticket_id"]


# ---------------------------------------------------------------------------
# 1. 坐席列表
# ---------------------------------------------------------------------------


def test_services_lists_two_seed_agents(client):
    resp = client.get("/api/b/services")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["demo_no_password"] is True
    assert len(body["items"]) == 2
    ids = [item["service_id"] for item in body["items"]]
    assert ids == ["seed-agent-1", "seed-agent-2"]
    names = {item["service_id"]: item["display_name"] for item in body["items"]}
    assert "客服" in names["seed-agent-1"]
    assert "客服" in names["seed-agent-2"]


# ---------------------------------------------------------------------------
# 2-3. 工单队列排序 / 结构 / 状态标签
# ---------------------------------------------------------------------------


def test_ticket_queue_queued_first_then_last_msg_desc(client, engine):
    merchant = "m-wb-queue"
    base = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    _add_ticket(engine, "tk-wb-q-old", merchant=merchant, status="queued", last_msg_at=base)
    _add_ticket(
        engine, "tk-wb-q-new", merchant=merchant, status="queued",
        last_msg_at=base + timedelta(hours=2),
    )
    _add_ticket(
        engine, "tk-wb-q-null", merchant=merchant, status="queued",
        last_msg_at=None, created_at=base + timedelta(hours=3),
    )
    _add_ticket(
        engine, "tk-wb-c-new", merchant=merchant, status="claimed",
        claimed_by="seed-agent-1", last_msg_at=base + timedelta(hours=5),
    )
    _add_ticket(
        engine, "tk-wb-c-old", merchant=merchant, status="claimed",
        claimed_by="seed-agent-2", last_msg_at=base + timedelta(hours=1),
    )

    body = client.get("/api/b/tickets", params={"merchant_id": merchant}).json()
    assert body["ok"] is True
    assert body["queued_first"] is True
    assert body["total"] == 5
    # queued 优先 → queued 组内 last_msg_at DESC(NULL 最后)→ 其它组同样规则
    assert [item["ticket_id"] for item in body["items"]] == [
        "tk-wb-q-new",
        "tk-wb-q-old",
        "tk-wb-q-null",
        "tk-wb-c-new",
        "tk-wb-c-old",
    ]


def test_ticket_list_structure_labels_and_status_filter(client, engine):
    merchant = "m-wb-labels"
    _add_ticket(
        engine, "tk-wb-label-q", merchant=merchant, status="queued",
        handoff_reason="explicit_request",
    )
    _add_ticket(
        engine, "tk-wb-label-c", merchant=merchant, status="claimed",
        claimed_by="seed-agent-1",
    )
    _add_ticket(
        engine, "tk-wb-label-w", merchant=merchant, status="waiting_customer",
        claimed_by="seed-agent-1",
    )
    _add_ticket(
        engine, "tk-wb-label-x", merchant=merchant, status="closed",
        claimed_by="seed-agent-1",
    )

    body = client.get("/api/b/tickets", params={"merchant_id": merchant}).json()
    items = body["items"]
    assert body["total"] == 4
    expected_labels = {
        "tk-wb-label-q": "待领取",
        "tk-wb-label-c": "跟进中",
        "tk-wb-label-w": "待客户回复",
        "tk-wb-label-x": "已关闭",
    }
    assert {item["ticket_id"]: item["status_label"] for item in items} == expected_labels

    ticket_keys = {
        "ticket_id", "merchant_id", "customer_id", "order_id", "status",
        "status_label", "claimed_by", "handoff_reason", "handoff_reason_label",
        "summary", "created_at_utc", "last_msg_at_utc", "message_count",
        "last_message",
    }
    for item in items:
        assert ticket_keys <= set(item)

    queued = next(item for item in items if item["ticket_id"] == "tk-wb-label-q")
    assert queued["handoff_reason"] == "explicit_request"
    assert queued["handoff_reason_label"] == "用户主动要求人工"
    assert queued["last_message"] is None
    closed = next(item for item in items if item["ticket_id"] == "tk-wb-label-x")
    assert closed["handoff_reason"] is None
    assert closed["handoff_reason_label"] is None

    filtered = client.get(
        "/api/b/tickets", params={"merchant_id": merchant, "status": "closed"}
    ).json()
    assert [item["ticket_id"] for item in filtered["items"]] == ["tk-wb-label-x"]
    assert filtered["total"] == 1


# ---------------------------------------------------------------------------
# 4-5. claim 成功 / 被抢
# ---------------------------------------------------------------------------


def test_claim_queued_ticket_returns_verified_receipt(client, engine):
    _add_ticket(engine, "tk-wb-claim", merchant="m-wb-claim", status="queued")
    resp = client.post(
        "/api/b/tickets/tk-wb-claim/claim", json={"service_id": "seed-agent-1"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    receipt = body["receipt"]
    assert receipt["ticket_id"] == "tk-wb-claim"
    assert receipt["status"] == "claimed"
    assert receipt["claimed_by"] == "seed-agent-1"
    assert receipt["verified"] is True
    assert receipt["occurred_at_utc"]

    row = client.get(
        "/api/b/tickets",
        params={"merchant_id": "m-wb-claim", "status": "claimed"},
    ).json()["items"]
    assert [item["ticket_id"] for item in row] == ["tk-wb-claim"]
    assert row[0]["claimed_by"] == "seed-agent-1"


def test_claim_twice_second_gets_conflict(client, engine):
    _add_ticket(engine, "tk-wb-race", merchant="m-wb-race", status="queued")
    first = client.post(
        "/api/b/tickets/tk-wb-race/claim", json={"service_id": "seed-agent-1"}
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/b/tickets/tk-wb-race/claim", json={"service_id": "seed-agent-2"}
    )
    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == "STATE_CONFLICT"
    assert "已被他人领取" in error["message"]


# ---------------------------------------------------------------------------
# 6. 详情含转人工后追加的买家消息
# ---------------------------------------------------------------------------


def test_ticket_detail_contains_post_handoff_buyer_messages(client):
    session_id, ticket_id = _handoff_and_confirm(client)
    for text in ("转人工后第一条", "转人工后第二条"):
        blocked = _chat(client, text, session_id=session_id)
        assert blocked["ai_replied"] is False, "转人工后必须走 blocked 轮"

    resp = client.get(f"/api/b/tickets/{ticket_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["ticket"]["ticket_id"] == ticket_id
    messages = body["messages"]
    buyer = [item for item in messages if item["author_type"] == "user"]
    assert [item["content"] for item in buyer] == ["转人工后第一条", "转人工后第二条"]
    assert all(item["author_label"] == "买家" for item in buyer)
    assert all(item["is_internal"] is False for item in buyer)
    message_keys = {
        "id", "author_type", "author_label", "content", "is_internal",
        "created_at_utc",
    }
    for item in messages:
        assert message_keys <= set(item)
    times = [item["created_at_utc"] for item in messages]
    assert times == sorted(times), "详情消息必须按 created_at asc"


# ---------------------------------------------------------------------------
# 7. 坐席回复:author=agent,买家 summary 可见
# ---------------------------------------------------------------------------


def test_agent_reply_visible_in_detail_and_buyer_summary(client):
    session_id, ticket_id = _handoff_and_confirm(client)
    claim = client.post(
        f"/api/b/tickets/{ticket_id}/claim", json={"service_id": "seed-agent-1"}
    )
    assert claim.status_code == 200, claim.text

    text = "您好,退款我来跟进,请稍候"
    resp = client.post(
        f"/api/b/tickets/{ticket_id}/messages",
        json={"service_id": "seed-agent-1", "content": text},
    )
    assert resp.status_code == 200, resp.text
    message = resp.json()["message"]
    assert message["author_type"] == "agent"
    assert message["author_label"] == "人工客服"
    assert message["content"] == text
    assert message["is_internal"] is False

    detail = client.get(f"/api/b/tickets/{ticket_id}").json()
    assert any(
        item["author_type"] == "agent" and item["content"] == text
        for item in detail["messages"]
    )

    summary = client.get(f"/api/c/handoff/sessions/{session_id}/summary").json()
    assert summary["ticket_status"] == "claimed"
    assert summary["ticket_closed"] is False
    assert any(
        item["author_type"] == "agent" and item["content"] == text
        for item in summary["ticket_messages"]
    )


# ---------------------------------------------------------------------------
# 8-9. 关单后的守卫
# ---------------------------------------------------------------------------


def test_close_then_reply_and_close_again_conflict(client, engine):
    _add_ticket(engine, "tk-wb-close", merchant="m-wb-close", status="queued")
    closed = client.post(
        "/api/b/tickets/tk-wb-close/close", json={"service_id": "seed-agent-1"}
    )
    assert closed.status_code == 200, closed.text
    receipt = closed.json()["receipt"]
    assert receipt["status"] == "closed"
    assert receipt["verified"] is True

    reply = client.post(
        "/api/b/tickets/tk-wb-close/messages",
        json={"service_id": "seed-agent-1", "content": "关单后还想回复"},
    )
    assert reply.status_code == 409, reply.text
    error = reply.json()["error"]
    assert error["code"] == "STATE_CONFLICT"
    assert "已关闭" in error["message"]

    again = client.post(
        "/api/b/tickets/tk-wb-close/close", json={"service_id": "seed-agent-2"}
    )
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "STATE_CONFLICT"


def test_claim_rejected_after_close(client, engine):
    _add_ticket(
        engine, "tk-wb-closed-claim", merchant="m-wb-claim-closed",
        status="closed", claimed_by="seed-agent-1",
    )
    resp = client.post(
        "/api/b/tickets/tk-wb-closed-claim/claim",
        json={"service_id": "seed-agent-2"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "STATE_CONFLICT"


# ---------------------------------------------------------------------------
# 10. 全链路:转人工 → blocked 追加 → 坐席回复 → 关单 → 买家 summary
# ---------------------------------------------------------------------------


def test_full_loop_buyer_summary_after_close(client):
    session_id, ticket_id = _handoff_and_confirm(client)

    blocked = _chat(client, "帮我催一下进度", session_id=session_id)
    assert blocked["ai_replied"] is False

    claim = client.post(
        f"/api/b/tickets/{ticket_id}/claim", json={"service_id": "seed-agent-1"}
    )
    assert claim.status_code == 200, claim.text
    reply = client.post(
        f"/api/b/tickets/{ticket_id}/messages",
        json={"service_id": "seed-agent-1", "content": "已为您处理,工单即将关闭"},
    )
    assert reply.status_code == 200, reply.text

    closed = client.post(
        f"/api/b/tickets/{ticket_id}/close", json={"service_id": "seed-agent-1"}
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["receipt"]["status"] == "closed"

    resp = client.get(f"/api/c/handoff/sessions/{session_id}/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("ticket", "ticket_link", "messages", "session_status"):
        assert key in body, f"summary 原有键不得丢失: {key}"
    assert body["ticket_closed"] is True
    assert body["ticket_status"] == "closed"
    messages = body["ticket_messages"]
    contents = [(item["author_type"], item["content"]) for item in messages]
    assert ("user", "帮我催一下进度") in contents
    assert ("agent", "已为您处理,工单即将关闭") in contents
    times = [item["created_at_utc"] for item in messages]
    assert times == sorted(times), "ticket_messages 必须按 created_at asc"


# ---------------------------------------------------------------------------
# 11-12. 404 / 参数校验
# ---------------------------------------------------------------------------


def test_ticket_detail_unknown_404(client):
    resp = client.get("/api/b/tickets/no-such-ticket")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_reply_blank_content_rejected(client, engine):
    _add_ticket(engine, "tk-wb-blank", merchant="m-wb-blank", status="queued")
    resp = client.post(
        "/api/b/tickets/tk-wb-blank/messages",
        json={"service_id": "seed-agent-1", "content": "   "},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 13. 工作台前端:可打开 + 无 CDN / 构建工具
# ---------------------------------------------------------------------------


def test_agent_frontend_served_without_cdn(client):
    page = client.get("/agent.html")
    assert page.status_code == 200, page.text
    assert "agent.js" in page.text
    assert "安心退" in page.text, "工作台必须展示对客品牌名"
    assert "客服工作台" in page.text

    js = client.get("/agent.js")
    assert js.status_code == 200, js.text
    assert client.get("/style.css").status_code == 200

    joined = (page.text + js.text + client.get("/style.css").text).lower()
    for banned in ("cdn.", "unpkg", "jsdelivr", "webpack", "vite", "npm "):
        assert banned not in joined, f"前端不得依赖 CDN/构建工具: {banned}"


# ===========================================================================
# W7-4 Copilot 建议回复(suggest):结构 / 禁词剔除 / GET 缓存 / 前端 DOM 静态断言
# ===========================================================================

from sqlalchemy import select  # noqa: E402  (W7-4 增量:校验 citations 来自真实条款)

from app.api import agent as agent_api  # noqa: E402
from app.guardrails import has_commitment_claim  # noqa: E402
from app.models import KnowledgeItem, TicketMessage  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_suggest_cache():
    """建议结果是进程内缓存;每个用例前清空,避免跨用例串数据。"""
    agent_api._SUGGEST_CACHE.clear()
    yield
    agent_api._SUGGEST_CACHE.clear()


def _add_ticket_message(
    engine,
    ticket_id: str,
    author_type: str,
    content: str,
    *,
    is_internal: bool = False,
    created_at: datetime | None = None,
) -> None:
    """直接落一条工单消息(绕过图,专测 suggest 的转录输入)。"""
    with Session(engine) as db:
        db.add(
            TicketMessage(
                ticket_id=ticket_id,
                author_type=author_type,
                content=content,
                is_internal=is_internal,
                created_at=created_at or datetime.now(timezone.utc),
            )
        )
        db.commit()


def _policy_pairs(engine) -> set[tuple[str, str]]:
    """商户知识库里的真实 (clause_id, version) 集合。"""
    with Session(engine) as db:
        rows = db.execute(
            select(KnowledgeItem.clause_id, KnowledgeItem.version).where(
                KnowledgeItem.merchant_id == MERCHANT_ID
            )
        ).all()
    return {(row[0], row[1]) for row in rows if row[0]}


def _make_suggest_ticket(engine, ticket_id: str, *, status: str = "claimed") -> str:
    """建一张有买家转录的工单(商户 seed-merchant-1,已加载政策语料)。"""
    _add_ticket(
        engine,
        ticket_id,
        merchant=MERCHANT_ID,
        status=status,
        claimed_by="seed-agent-1" if status != "queued" else None,
        summary="买家反馈商品质量问题,要求退款",
    )
    _add_ticket_message(engine, ticket_id, "user", "收到的锅是坏的,我要退款,这是质量问题")
    _add_ticket_message(engine, ticket_id, "agent", "您好,我帮您看看")
    return ticket_id


def test_suggest_post_structure_and_real_citations(client, engine):
    ticket_id = _make_suggest_ticket(engine, "tk-wb-suggest-1")
    resp = client.post(f"/api/b/tickets/{ticket_id}/suggest")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]

    for key in (
        "summary", "suggested_reply", "citations", "mock", "model", "generated_at",
    ):
        assert key in data, f"suggest 响应缺少键: {key}"
    assert data["mock"] is True
    assert data["model"] == "rule-suggest-v1 (MOCK)"
    assert data["ticket_id"] == ticket_id
    assert data["generated_at"]

    assert data["summary"].strip(), "摘要不能为空"
    assert len(data["summary"]) <= 120, "摘要必须 ≤120 字"
    assert data["suggested_reply"].strip(), "建议回复不能为空"
    assert not has_commitment_claim(data["suggested_reply"]), "建议回复不得含承诺性话术"
    assert not has_commitment_claim(data["summary"]), "摘要不得含承诺性话术"

    # citations 必须来自真实的 kb 条款(条款号 + 版本都能在库里对上)
    assert data["citations"], "质量问题查询应命中政策条款"
    pairs = _policy_pairs(engine)
    for citation in data["citations"]:
        assert set(citation) == {"clause_id", "version", "score"}
        assert (citation["clause_id"], citation["version"]) in pairs
        assert citation["score"] > 0
    # 摘要里要带命中的条款 id(任务书:含关键诉求与命中的条款 id)
    assert any(c["clause_id"] in data["summary"] for c in data["citations"])


def test_suggest_strips_commitment_sentences_via_monkeypatch(client, engine, monkeypatch):
    """monkeypatch 模板注入承诺句 → 整句剔除;全被剔除时给确定性兜底话术。"""
    ticket_id = _make_suggest_ticket(engine, "tk-wb-suggest-strip")

    def _draft(*args, **kwargs):
        return {
            "summary": "买家要求退款。退款将在 3 个工作日内到账。",
            "suggested_reply": "退款将在 3 个工作日内到账。我们赔偿 200 元。",
        }

    monkeypatch.setattr(agent_api, "_compose_suggestion", _draft)
    resp = client.post(f"/api/b/tickets/{ticket_id}/suggest")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert "3 个工作日" not in data["suggested_reply"]
    assert "200 元" not in data["suggested_reply"]
    assert data["suggested_reply"] == agent_api._SUGGEST_FALLBACK_REPLY
    assert "尽快核实处理" in data["suggested_reply"]
    assert not has_commitment_claim(data["suggested_reply"])

    # 摘要里只剔除含承诺的那句,其余保留
    assert "3 个工作日" not in data["summary"]
    assert data["summary"] == "买家要求退款。"
    assert not has_commitment_claim(data["summary"])


def test_suggest_closed_ticket_conflict(client, engine):
    ticket_id = _make_suggest_ticket(engine, "tk-wb-suggest-closed", status="closed")
    resp = client.post(f"/api/b/tickets/{ticket_id}/suggest")
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "STATE_CONFLICT"
    assert "已关闭" in error["message"]


def test_suggest_unknown_ticket_404(client):
    post = client.post("/api/b/tickets/no-such-suggest/suggest")
    assert post.status_code == 404, post.text
    assert post.json()["error"]["code"] == "NOT_FOUND"
    get = client.get("/api/b/tickets/no-such-suggest/suggest")
    assert get.status_code == 404, get.text
    assert get.json()["error"]["code"] == "NOT_FOUND"


def test_suggest_get_returns_cache_only_after_post(client, engine):
    """GET 无缓存 → data:null;POST 后 GET 返回最近一次结果;重复 GET 无副作用。"""
    ticket_id = _make_suggest_ticket(engine, "tk-wb-suggest-cache")
    path = f"/api/b/tickets/{ticket_id}/suggest"

    first = client.get(path)
    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True, "data": None}
    second = client.get(path)
    assert second.json() == {"ok": True, "data": None}, "GET 不得写缓存(无副作用)"

    posted = client.post(path)
    assert posted.status_code == 200, posted.text
    generated = posted.json()["data"]

    cached = client.get(path).json()
    assert cached["ok"] is True
    assert cached["data"]["suggested_reply"] == generated["suggested_reply"]
    assert cached["data"]["summary"] == generated["summary"]
    assert cached["data"]["generated_at"] == generated["generated_at"]
    assert cached["data"]["mock"] is True
    # 再 GET 一次,结果保持不变(仍无副作用)
    assert client.get(path).json() == cached


def test_agent_workbench_suggest_dom_keywords():
    """静态断言:TestClient 不执行 JS,只校验前端源码里的按钮 id / 绑定 / 填入目标。"""
    root = Path(__file__).resolve().parents[2]
    html = (root / "frontend" / "agent.html").read_text(encoding="utf-8")
    js = (root / "frontend" / "agent.js").read_text(encoding="utf-8")

    for dom_id in (
        "suggestPanel", "suggest-btn", "suggest-summary", "suggest-reply",
        "fill-reply-btn",
    ):
        assert f'id="{dom_id}"' in html, f"工作台详情面板缺少 DOM id: {dom_id}"
        assert f'getElementById("{dom_id}")' in js, f"agent.js 未绑定 DOM id: {dom_id}"

    assert 'els.suggestBtn.addEventListener("click"' in js
    assert 'els.fillReplyBtn.addEventListener("click"' in js
    assert '"/suggest"' in js, "agent.js 必须调用 suggest 接口"
    assert "payload.suggested_reply" in js, "前端必须读取建议回复字段"
    assert 'textarea.id = "reply-input"' in js
    assert 'querySelector("#reply-input")' in js
    assert ".value = els.suggestReply.value" in js

    # 一键填入只写回复框,不自动发送(messages 接口不得出现在填入逻辑里)
    fill_block = js.split("function fillReplyFromSuggestion()")[1].split(
        "/* --------------------------------- 动作"
    )[0]
    assert "/messages" not in fill_block, "填入建议不得自动发送回复"


# ---------------------------------------------------------------------------
# G1(2026-11-18 第四轮审计):内部备注不得进模型 prompt
#
# 缺陷:两条 prompt 输入路径口径不一致——
#   _suggest_user_texts  过滤 `and not message.is_internal`  → 内部备注不进检索 query
#   _suggest_transcript  **不过滤**                          → 内部备注进模型 prompt
# 而转录每行只标 author_type、**不标 is_internal**,提示词里也没有任何"禁止对客引用"
# 的指令,所以内部备注在模型看来与"客服已经对客户说过的话"完全一样。
# 输出侧两道护栏(去条款代号 / 去承诺话术)都不匹配风控评价,拦不住。
#
# 用户裁定选项二:**拼装 prompt 前直接过滤掉内部备注**,并让两条路径共用同一口径。
# 坐席在工作台同一屏本来就看得到内部备注,失去的只是模型的上下文。
# ---------------------------------------------------------------------------

G1_INTERNAL = "内部备注:该买家疑似恶意退款,先拖延处理,勿告知客户"
G1_PUBLIC_AGENT = "您好,我帮您看看"
G1_BUYER_ASK = "收到的锅是坏的,我要退款,这是质量问题"


def test_suggest_prompt_excludes_internal_notes(client, engine):
    """G1:内部备注原文不得出现在 suggest 的模型 prompt 里。

    prompt 由响应原样回传(agent.py 的 data["prompt"]),所以这是行为级观测,
    不是打桩推测。
    """
    ticket_id = _make_suggest_ticket(engine, "tk-g1-internal")
    _add_ticket_message(engine, ticket_id, "agent", G1_INTERNAL, is_internal=True)
    resp = client.post(f"/api/b/tickets/{ticket_id}/suggest")
    assert resp.status_code == 200, resp.text
    prompt = str(resp.json()["data"]["prompt"])
    assert G1_INTERNAL not in prompt, "内部备注原文不得进模型 prompt"
    assert "疑似恶意退款" not in prompt, "内部风控评价不得以任何片段进 prompt"


def test_suggest_prompt_keeps_public_transcript(client, engine):
    """正向:公开对话必须照常进 prompt(防守卫过度把转录清空)。"""
    ticket_id = _make_suggest_ticket(engine, "tk-g1-public")
    _add_ticket_message(engine, ticket_id, "agent", G1_INTERNAL, is_internal=True)
    resp = client.post(f"/api/b/tickets/{ticket_id}/suggest")
    assert resp.status_code == 200, resp.text
    prompt = str(resp.json()["data"]["prompt"])
    assert G1_BUYER_ASK in prompt, "买家提问必须仍在 prompt 里"
    assert G1_PUBLIC_AGENT in prompt, "坐席公开回复必须仍在 prompt 里"


def test_suggest_two_input_paths_share_one_internal_filter(client, engine):
    """G1 口径统一:检索输入与 prompt 转录对内部备注的取舍必须一致。

    直接断言两条路径共用同一判定,而不是各写一份过滤——
    「同一概念两处定义必分叉」是本轮 11 条发现里 3 条的共同病根。
    """
    ticket_id = _make_suggest_ticket(engine, "tk-g1-paths")
    _add_ticket_message(engine, ticket_id, "agent", G1_INTERNAL, is_internal=True)
    with Session(engine) as db:
        messages = list(
            db.scalars(
                select(TicketMessage)
                .where(TicketMessage.ticket_id == ticket_id)
                .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
            )
        )
    user_texts = agent_api._suggest_user_texts(messages)
    transcript = agent_api._suggest_transcript(messages)
    assert not any(G1_INTERNAL in t for t in user_texts), "检索输入不得含内部备注"
    assert G1_INTERNAL not in transcript, "prompt 转录不得含内部备注"
    # 两条路径必须调同一个判定函数(口径单一事实源)
    assert hasattr(agent_api, "_model_visible"), "应有共用的模型输入可见性判定"
    internal_row = next(m for m in messages if m.content == G1_INTERNAL)
    public_row = next(m for m in messages if m.content == G1_PUBLIC_AGENT)
    assert agent_api._model_visible(internal_row) is False
    assert agent_api._model_visible(public_row) is True
