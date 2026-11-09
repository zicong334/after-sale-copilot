"""test_handoff_flow.py — W3-C 转人工全链路验收(≥8 条)。

覆盖:
1. 转人工 → handed_off → 再发消息 AI 不答(图未运行,trace 只有 route__blocked);
2. SSE 四类事件 + usage{estimated:true}:普通问答 / 退款确认卡 / 转人工横幅;
3. 客服侧:队列 queued 优先、claim 并发只有一个成功(被抢 409「已被他人领取」)、
   关单守卫、会话摘要;
4. 前端聊天壳:GET / 与 /chat.html 均 200,且无 CDN / 构建工具。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import get_db
from app.errors import StateConflictError
from app.services import tickets as ticket_svc
from app.graph import build as graph_build
from app.main import app
from app.models import ChatSession, Merchant, Ticket, TicketMessage
from app.trace import EVENT_ROUTE_BLOCKED, trace_events
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER = "seed-cust-001"
HANDOFF_TEXT = "我不跟你聊了,直接给我转人工客服"
ELIGIBLE_TEXT = "3 天前收到的衣服有线头,是质量问题,我要退货退款"
POLICY_TEXT = "退货的话,来回运费是谁出？"
REFUND_CONTEXT = {"order_id": "seed-order-020", "days_since_delivery": 3}
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


# ---------------------------------------------------------------------------
# fixtures
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


def post_chat(client: TestClient, text: str, *, customer_id: str = CUSTOMER,
              context: dict | None = None, session_id: str | None = None) -> dict:
    body = {"merchant_id": MERCHANT_ID, "customer_id": customer_id, "text": text, "context": context}
    if session_id:
        body["session_id"] = session_id
    resp = client.post("/api/c/chat", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def handoff_and_confirm(client: TestClient) -> dict:
    """转人工 → 确认建单;返回含 ticket_id 的确认响应。"""
    first = post_chat(client, HANDOFF_TEXT)
    token = first["pending_confirm"]["confirm_token"]
    resp = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": first["session_id"]})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    out["session_id"] = first["session_id"]
    return out


def parse_sse(text: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for raw in text.split("\n\n"):
        event = None
        data = None
        for line in raw.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if event and data is not None:
            frames.append((event, data))
    return frames


# ---------------------------------------------------------------------------
# 1. handed_off 后 AI 停止回复(图未运行)
# ---------------------------------------------------------------------------


def test_handoff_then_ai_stops_without_running_graph(client, engine):
    first = post_chat(client, HANDOFF_TEXT)
    thread_id = first["session_id"]
    with Session(engine) as db:
        assert db.get(ChatSession, thread_id).status == "handed_off"
        assert first["pending_confirm"], "建单前必须先出确认卡"

    # 第二次发消息:会话已 handed_off → AI 真静默(不回固定话术),不跑图
    second = post_chat(client, "我还在吗？", session_id=thread_id)
    assert second["ai_replied"] is False
    assert second["intent"] == "blocked"
    assert second["classification_path"] == "route_guard"
    assert second["reply"] == ""
    assert "usage" not in second, "blocked 轮没有 AI 生成,不得带 usage"
    assert second["ack"] == "消息已记录,坐席会尽快跟进。"
    assert second["ack_ticket_id"] is None, "还没确认建单,没有可追加的工单"
    assert second["tool_calls"] == []
    assert second["session_status"] == "handed_off"

    with Session(engine) as db:
        events = trace_events(db, second["trace_id"])
        assert [e.tool_name for e in events] == [EVENT_ROUTE_BLOCKED], "图未运行,只允许 1 条拦截事件"
        names = [e.tool_name for e in trace_events(db, first["trace_id"])]
        assert "router__classify" in names, "首轮确实跑过图"


def test_handoff_confirm_ticket_enters_queue_and_summary(client, engine):
    out = handoff_and_confirm(client)
    ticket_id = out["receipt"]["ticket_id"]
    session_id = out["session_id"]

    queue = client.get("/api/c/handoff/tickets", params={"merchant_id": MERCHANT_ID})
    assert queue.status_code == 200
    items = queue.json()["items"]
    assert items, "队列不得为空"
    assert items[0]["status"] == "queued", "queued 必须排在最前"
    assert ticket_id in [item["ticket_id"] for item in items]

    summary = client.get(f"/api/c/handoff/sessions/{session_id}/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["session_status"] == "handed_off"
    assert body["ticket"] is not None
    assert body["messages"], "摘要里要带最近消息"
    assert "ticket_link" in body


# ---------------------------------------------------------------------------
# 2. SSE 事件流
# ---------------------------------------------------------------------------


def test_stream_policy_answer_events_and_usage(client):
    resp = client.post(
        "/api/c/chat/stream",
        json={"merchant_id": MERCHANT_ID, "customer_id": CUSTOMER, "text": POLICY_TEXT},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(resp.text)
    names = [name for name, _ in frames]
    assert "message" in names and "usage" in names and "done" in names
    assert names.count("citation") >= 1, "政策问答必须带条款引用事件"
    usage = next(data for name, data in frames if name == "usage")
    assert usage["estimated"] is True
    assert usage["tokens_in"] > 0 and usage["tokens_out"] > 0
    assert usage["cost_usd"] >= 0 and "latency_ms" in usage
    done = next(data for name, data in frames if name == "done")
    assert done["ok"] is True and done["session_id"] and done["trace_id"]
    # message 分片拼起来 == 完整回复
    reply = "".join(data["delta"] for name, data in frames if name == "message")
    assert reply == done["reply"]


def test_stream_refund_emits_confirm_card(client):
    resp = client.post(
        "/api/c/chat/stream",
        json={"merchant_id": MERCHANT_ID, "customer_id": "seed-cust-010", "text": ELIGIBLE_TEXT,
              "context": REFUND_CONTEXT},
    )
    assert resp.status_code == 200
    frames = parse_sse(resp.text)
    card = [data for name, data in frames if name == "confirm_request"]
    assert len(card) == 1
    card = card[0]
    for key in ("what", "impact", "irreversibility", "alternatives"):
        assert card[key].strip(), f"确认卡四要素缺失: {key}"
    assert card["confirm_token"]
    assert card["thread_id"], "SSE 确认卡要带 thread_id,前端确认时用"


def test_stream_handoff_emits_notice(client):
    resp = client.post(
        "/api/c/chat/stream",
        json={"merchant_id": MERCHANT_ID, "customer_id": CUSTOMER, "text": HANDOFF_TEXT},
    )
    assert resp.status_code == 200
    frames = parse_sse(resp.text)
    notice = [data for name, data in frames if name == "handoff_notice"]
    assert len(notice) == 1
    assert notice[0]["session_status"] == "handed_off"
    done = next(data for name, data in frames if name == "done")
    assert done["session_status"] == "handed_off"
    assert done["ai_replied"] is True  # 首轮还说话,确认后才停答


def test_stream_handed_off_emits_append_ack_without_usage(client):
    """W5 真静默:handed_off 后的 SSE 只有 append_ack + done(零 message/usage/横幅)。"""
    first = post_chat(client, HANDOFF_TEXT)
    resp = client.post(
        "/api/c/chat/stream",
        json={"merchant_id": MERCHANT_ID, "customer_id": CUSTOMER,
              "text": "再问一句", "session_id": first["session_id"]},
    )
    assert resp.status_code == 200
    frames = parse_sse(resp.text)
    names = [name for name, _ in frames]
    assert names.count("append_ack") == 1, names
    assert "handoff_notice" not in names, "横幅只在转人工创建当轮发一次"
    assert "usage" not in names
    assert "message" not in names, "blocked 轮不得有任何 AI 文本分片"
    ack = next(data for name, data in frames if name == "append_ack")
    assert ack["ack"]
    assert ack["session_status"] == "handed_off"
    assert ack["ticket_id"] is None  # 尚未确认建单
    done = next(data for name, data in frames if name == "done")
    assert done["ai_replied"] is False
    assert done["reply"] == ""
    assert "usage" not in done, "blocked 轮的 done 不得带 usage 键"
    assert done["ack"]


def test_three_blocked_messages_append_to_ticket_and_notice_once(client, engine):
    """连发 3 条:每轮零 AI 文本/零 usage/1 次 append_ack;买家消息全部进工单;横幅仅 1 次。"""
    first = client.post(
        "/api/c/chat/stream",
        json={"merchant_id": MERCHANT_ID, "customer_id": CUSTOMER, "text": HANDOFF_TEXT},
    )
    assert first.status_code == 200, first.text
    first_frames = parse_sse(first.text)
    first_names = [name for name, _ in first_frames]
    assert first_names.count("handoff_notice") == 1, "转人工创建当轮必须发横幅"
    # 横幅携带 reply_stopped=False(当轮 AI 还说话),确认后才彻底静默
    notice = next(data for name, data in first_frames if name == "handoff_notice")
    assert notice["reply_stopped"] is False
    session_id = next(data for name, data in first_frames if name == "done")["session_id"]
    token = next(
        data for name, data in first_frames if name == "confirm_request"
    )["confirm_token"]
    confirmed = client.post(
        f"/api/c/confirm/{token}/confirm", json={"thread_id": session_id}
    )
    assert confirmed.status_code == 200, confirmed.text
    ticket_id = confirmed.json()["receipt"]["ticket_id"]

    texts = ["转人工后第一条", "转人工后第二条", "转人工后第三条"]
    notice_count = first_names.count("handoff_notice")
    for text in texts:
        resp = client.post(
            "/api/c/chat/stream",
            json={
                "merchant_id": MERCHANT_ID,
                "customer_id": CUSTOMER,
                "text": text,
                "session_id": session_id,
            },
        )
        assert resp.status_code == 200, resp.text
        frames = parse_sse(resp.text)
        names = [name for name, _ in frames]
        assert names.count("append_ack") == 1, names
        assert names.count("handoff_notice") == 0
        assert "message" not in names and "usage" not in names
        ack = next(data for name, data in frames if name == "append_ack")
        assert ack["ticket_id"] == ticket_id, "买家消息必须真正追加到该工单"
        done = next(data for name, data in frames if name == "done")
        assert done["reply"] == "" and done["ai_replied"] is False
        assert "usage" not in done
        assert done["ack_ticket_id"] == ticket_id
        notice_count += names.count("handoff_notice")
    assert notice_count == 1, "整条流程只允许 1 次 handoff_notice"

    with Session(engine) as db:
        rows = list(
            db.scalars(
                select(TicketMessage)
                .where(
                    TicketMessage.ticket_id == ticket_id,
                    TicketMessage.author_type == "user",
                )
                .order_by(TicketMessage.created_at.asc())
            )
        )
    assert [m.content for m in rows] == texts
    assert all(m.is_internal is False for m in rows)


# ---------------------------------------------------------------------------
# 3. 客服侧:claim 并发 / 守卫 / 队列
# ---------------------------------------------------------------------------


def _add_ticket(engine, ticket_id: str, *, status: str = "queued", merchant: str = "m-hf") -> None:
    with Session(engine) as db:
        if db.get(Merchant, merchant) is None:
            db.add(Merchant(id=merchant, name="转人工测试店"))
        db.add(
            Ticket(
                id=ticket_id,
                merchant_id=merchant,
                customer_id="c-hf",
                status=status,
                claimed_by="agent-seeded" if status == "claimed" else None,
            )
        )
        db.commit()


def test_claim_queued_and_second_claim_loses(client, engine):
    _add_ticket(engine, "tk-hf-1")
    first = client.post("/api/c/handoff/tickets/tk-hf-1/claim", json={"service_id": "agent-1"})
    assert first.status_code == 200, first.text
    receipt = first.json()["receipt"]
    assert receipt["status"] == "claimed"
    assert receipt["claimed_by"] == "agent-1"
    assert receipt["verified"] is True

    second = client.post("/api/c/handoff/tickets/tk-hf-1/claim", json={"service_id": "agent-2"})
    assert second.status_code == 409
    assert "已被他人领取" in second.json()["error"]["message"]


def test_claim_concurrent_threads_only_one_wins(seeded, engine):
    """真并发:两个线程各自 Session 同时 claim 同一工单,恰好一个成功、一个 409。

    SQLite 写锁竞争可能抛 OperationalError(database is locked),用短重试消化;
    重试后仍由守卫式 UPDATE 的 rowcount 判定胜负,不改变"只有一个成功"的语义。
    """
    _add_ticket(engine, "tk-hf-race")
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(service_id: str) -> None:
        with Session(engine) as db:
            barrier.wait()
            for attempt in range(5):
                try:
                    ticket = ticket_svc.claim(db, "tk-hf-race", service_id)
                    result = ("ok", ticket.claimed_by or "")
                    break
                except OperationalError:
                    db.rollback()
                    time.sleep(0.02 * (attempt + 1))
                except StateConflictError:
                    result = ("conflict", service_id)
                    break
            else:  # pragma: no cover - 兜底失败信息
                result = ("error", service_id)
            with lock:
                outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(kind for kind, _ in outcomes) == ["conflict", "ok"], outcomes
    winner = next(name for kind, name in outcomes if kind == "ok")
    with Session(engine) as db:
        ticket = db.get(Ticket, "tk-hf-race")
        assert ticket.status == "claimed"
        assert ticket.claimed_by == winner


def test_close_guard_and_claim_after_close(client, engine):
    _add_ticket(engine, "tk-hf-2")
    closed = client.post("/api/c/handoff/tickets/tk-hf-2/close", json={"service_id": "agent-1"})
    assert closed.status_code == 200
    assert closed.json()["receipt"]["status"] == "closed"
    again = client.post("/api/c/handoff/tickets/tk-hf-2/close", json={"service_id": "agent-1"})
    assert again.status_code == 409
    claim = client.post("/api/c/handoff/tickets/tk-hf-2/claim", json={"service_id": "agent-2"})
    assert claim.status_code == 409


def test_queue_queued_first_and_filter(client, engine):
    _add_ticket(engine, "tk-hf-claimed", status="claimed", merchant="m-hf-order")
    _add_ticket(engine, "tk-hf-queued", status="queued", merchant="m-hf-order")
    resp = client.get("/api/c/handoff/tickets", params={"merchant_id": "m-hf-order"})
    items = resp.json()["items"]
    assert [item["ticket_id"] for item in items] == ["tk-hf-queued", "tk-hf-claimed"]
    filtered = client.get("/api/c/handoff/tickets", params={"merchant_id": "m-hf-order", "status": "claimed"})
    assert [item["ticket_id"] for item in filtered.json()["items"]] == ["tk-hf-claimed"]


def test_summary_unknown_session_404(client):
    resp = client.get("/api/c/handoff/sessions/no-such-session/summary")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_summary_hides_internal_agent_notes(client, engine):
    """买家侧会话摘要不下发坐席内部备注(is_internal=True),只下发买家可见消息。

    前端 app.js 只是客户端过滤,接口本身必须过滤 —— 这里断言 ticket_messages
    里没有内部备注内容,但普通坐席回复仍可见。
    """
    with Session(engine) as db:
        # 复用 seed 里已存在的 merchant/customer(chat_session 有外键),
        # 且 seed-cust-001 无工单 —— 本测试建的单就是该客户最近一张。
        db.add(ChatSession(id="s-hf-internal", merchant_id=MERCHANT_ID, customer_id=CUSTOMER))
        db.add(Ticket(id="tk-hf-internal", merchant_id=MERCHANT_ID, customer_id=CUSTOMER, status="queued"))
        db.add_all(
            [
                TicketMessage(id="tm-hf-public", ticket_id="tk-hf-internal", author_type="agent", content="您好,我来跟进"),
                TicketMessage(
                    id="tm-hf-internal",
                    ticket_id="tk-hf-internal",
                    author_type="agent",
                    content="内部备注:该买家伙同惯犯,注意核实",
                    is_internal=True,
                ),
            ]
        )
        db.commit()

    resp = client.get("/api/c/handoff/sessions/s-hf-internal/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    contents = [item["content"] for item in body["ticket_messages"]]
    assert "您好,我来跟进" in contents
    assert all("内部备注" not in text for text in contents), "内部备注不得下发买家侧"
    assert all(item["is_internal"] is False for item in body["ticket_messages"])


# ---------------------------------------------------------------------------
# 4. 前端壳:可打开 + 无 CDN / 构建工具
# ---------------------------------------------------------------------------


def test_frontend_served_without_build_tools(client):
    root = client.get("/")
    assert root.status_code == 200
    assert "售后 Copilot" in root.text
    page = client.get("/chat.html")
    assert page.status_code == 200
    assert "app.js" in page.text
    assets = {
        "/app.js": client.get("/app.js"),
        "/style.css": client.get("/style.css"),
    }
    assert all(resp.status_code == 200 for resp in assets.values())
    joined = root.text + assets["/app.js"].text + assets["/style.css"].text
    for banned in ("cdn.", "unpkg", "jsdelivr", "webpack", "vite", "npm "):
        assert banned not in joined.lower(), f"前端不得依赖 CDN/构建工具: {banned}"
