"""test_confirm.py — W3-C 确认卡闭环验收(≥10 条)。

覆盖:
1. 待确认卡四要素 + token + 倒计时,按 thread_id 读取(checkpointer 投影);
2. 确认 → 退款单恰好一张 + 回执 **回读 DB** verified=true + 终态回复;
3. 重复确认幂等(不重复执行);拒绝 → 零写操作 + 草稿保留;拒绝后再确认 409;
4. 超时 10 分钟(假时钟)= 拒绝 + 草稿保留(三个入口:GET pending / POST confirm / POST reject);
5. token 与 thread_id 不匹配 409;已决卡再取 404;未知会话 404。
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.graph import build as graph_build
from app.main import app
from app.models import ChatMessage, ChatSession, RefundRequest, Ticket
from app.tools.events import get_confirm
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER_REFUND = "seed-cust-010"
CUSTOMER_HANDOFF = "seed-cust-001"
ELIGIBLE_TEXT = "3 天前收到的衣服有线头,是质量问题,我要退货退款"
HANDOFF_TEXT = "我不跟你聊了,直接给我转人工客服"
REFUND_CONTEXT = {"order_id": "seed-order-020", "days_since_delivery": 3}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    with Session(engine) as session:
        seed(session)
        session.commit()


@pytest.fixture()
def ckpt(tmp_dir, monkeypatch) -> Path:
    """图断点库隔离到 .tmp:chat 与 confirm 都按运行期常量解析。"""
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


def post_chat(client: TestClient, text: str, *, customer_id: str = CUSTOMER_REFUND,
              context: dict | None = None, session_id: str | None = None) -> dict:
    body = {
        "merchant_id": MERCHANT_ID,
        "customer_id": customer_id,
        "text": text,
        "context": context,
    }
    if session_id:
        body["session_id"] = session_id
    resp = client.post("/api/c/chat", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def start_refund(client: TestClient) -> dict:
    """跑一条合格退款,停在确认门;返回首轮响应(含 pending_confirm)。"""
    body = post_chat(client, ELIGIBLE_TEXT, context=REFUND_CONTEXT)
    assert body["pending_confirm"], "合格退款必须停在确认门"
    return body


def pending(client: TestClient, thread_id: str) -> dict:
    resp = client.get("/api/c/confirm/pending", params={"thread_id": thread_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def count(db_session: Session, model) -> int:
    return int(db_session.scalar(select(func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------------------
# 1. 待确认卡:四要素 + token + 倒计时(按 thread_id)
# ---------------------------------------------------------------------------


def test_pending_card_has_four_elements(client, engine):
    body = start_refund(client)
    thread_id = body["session_id"]
    card = pending(client, thread_id)

    assert card["status"] == "pending"
    assert card["confirm_token"] == body["pending_confirm"]["confirm_token"]
    assert card["tool"] == "refund__create"
    assert set(card["card"]) == {"what", "impact", "irreversibility", "alternatives"}
    assert all(card["card"][key].strip() for key in card["card"]), "四要素不得为空"
    assert "退款" in card["card"]["what"] and "seed-order-020" in card["card"]["what"]
    assert 0 < card["expires_in_seconds"] <= 600
    assert card["draft"]["args"]["amount_cents"] > 0
    assert card["draft"]["status"] == "pending"


def test_pending_unknown_thread_404(client):
    resp = client.get("/api/c/confirm/pending", params={"thread_id": "no-such-thread"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_confirm_token_thread_mismatch_409(client):
    first = start_refund(client)
    second = start_refund(client)
    token = first["pending_confirm"]["confirm_token"]
    resp = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": second["session_id"]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFIRM_THREAD_MISMATCH"


# ---------------------------------------------------------------------------
# 2. 确认 → 恰好一张退款单 + verified 回执 + 终态回复
# ---------------------------------------------------------------------------


def test_confirm_creates_exactly_one_refund_with_verified_receipt(client, engine):
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    assert count(Session(engine), RefundRequest) == 0, "确认前不得有写副作用"

    resp = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": thread_id})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    receipt = out["receipt"]
    assert out["decision"] == "confirmed"
    assert receipt["kind"] == "refund"
    assert receipt["verified"] is True, "verified 必须是回读 DB 的结果"
    assert receipt["status"] == "申请"
    assert receipt["order_id"] == "seed-order-020"
    assert receipt["amount_cents"] > 0
    assert receipt["occurred_at_utc"]
    assert receipt["trace_id"] == out["trace_id"]
    assert "退款单号" in out["reply"]

    with Session(engine) as db:
        assert count(db, RefundRequest) == 1
        refund = db.scalars(select(RefundRequest)).one()
        assert refund.id == receipt["refund_request_id"]
        assert refund.status == "申请"
        card = get_confirm(db, token)
        assert card is not None and card.status == "confirmed"
        # 会话仍是 active,但 assistant 终态消息已补记
        assert db.get(ChatSession, thread_id).status == "active"


def test_confirm_twice_is_idempotent(client, engine):
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    first = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": thread_id}).json()
    second = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": thread_id}).json()

    assert first["receipt"]["refund_request_id"] == second["receipt"]["refund_request_id"]
    assert second["idempotent"] is True
    assert second["receipt"]["verified"] is True
    with Session(engine) as db:
        assert count(db, RefundRequest) == 1, "重复确认不得重复执行"


def test_pending_after_confirm_404(client):
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": thread_id})
    resp = client.get("/api/c/confirm/pending", params={"thread_id": thread_id})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CONFIRM_NOT_PENDING"


# ---------------------------------------------------------------------------
# 3. 拒绝 / 冲突
# ---------------------------------------------------------------------------


def test_reject_leaves_zero_write_and_keeps_draft(client, engine):
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]

    resp = client.post(f"/api/c/confirm/{token}/reject", json={"thread_id": thread_id})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["decision"] == "rejected"
    assert out["receipt"] is None
    assert set(out["draft"]["card"]) == {"what", "impact", "irreversibility", "alternatives"}
    assert out["draft"]["args"]["amount_cents"] > 0, "拒绝后草稿(待执行参数)必须保留"
    assert "已取消" in out["reply"]

    with Session(engine) as db:
        assert count(db, RefundRequest) == 0
        card = get_confirm(db, token)
        assert card is not None and card.status == "cancelled"
        assert card.payload, "payload 草稿必须保留"


def test_confirm_after_reject_is_conflict(client, engine):
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    client.post(f"/api/c/confirm/{token}/reject", json={"thread_id": thread_id})

    resp = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": thread_id})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "STATE_CONFLICT"
    with Session(engine) as db:
        assert count(db, RefundRequest) == 0


def test_reject_twice_is_idempotent(client):
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    client.post(f"/api/c/confirm/{token}/reject", json={"thread_id": thread_id})
    second = client.post(f"/api/c/confirm/{token}/reject", json={"thread_id": thread_id})
    assert second.status_code == 200
    assert second.json()["idempotent"] is True


def test_reject_concurrent_is_single_shot(client, engine):
    """W7-6:同一张卡的并发 reject 只有一个真正执行,其余回放幂等回执。

    四个线程同时进入 reject:每 token 一把锁把"读状态 → resume → 置 cancelled"
    变成临界区;第一个赢家真正落终态,其余在锁内重读后走 cancelled 幂等回执。
    断言:没有异常/409(win32 上由 API 抛出的 StateConflictError 会进 errors)、
    ``idempotent=False`` 恰好一次、确认卡只迁移一次、assistant 终态回复只补一条。
    """
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]

    from app.api import confirm as confirm_api

    workers = 4
    barrier = threading.Barrier(workers)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        db = Session(engine)
        try:
            barrier.wait(timeout=5)
            out = confirm_api.reject(
                token, confirm_api.ThreadRef(thread_id=thread_id), db=db
            )
            results.append(out)
        except BaseException as exc:  # noqa: BLE001 - 并发下任何异常都算失败
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == [], f"并发 reject 不应抛异常/409: {errors!r}"
    assert len(results) == workers
    assert all(out["decision"] == "rejected" for out in results)
    assert sum(1 for out in results if not out["idempotent"]) == 1, results

    with Session(engine) as db:
        assert count(db, RefundRequest) == 0
        card = get_confirm(db, token)
        assert card is not None and card.status == "cancelled"
        reject_replies = db.scalars(
            select(ChatMessage).where(ChatMessage.intent == "confirm_reject")
        ).all()
        assert len(reject_replies) == 1, "拒绝终态回复只能补一条(不双写)"


# ---------------------------------------------------------------------------
# 4. 超时 = 拒绝 + 草稿保留(假时钟)
# ---------------------------------------------------------------------------


def expire_clock(monkeypatch) -> None:
    """假时钟:拨到 10 分钟 TTL 之后(卡过期)。"""
    from app.api import confirm as confirm_api

    monkeypatch.setattr(confirm_api, "_now", lambda: utc_now() + timedelta(minutes=11))


def test_timeout_on_pending_rejects_and_keeps_draft(client, engine, monkeypatch):
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    expire_clock(monkeypatch)

    out = pending(client, thread_id)  # 打开确认卡时发现已超时
    assert out["status"] == "expired"
    assert out["decision"] == "rejected"
    assert out["timeout"] is True
    assert out["draft"]["status"] == "expired"
    assert set(out["draft"]["card"]) == {"what", "impact", "irreversibility", "alternatives"}
    assert out["draft"]["args"]["amount_cents"] > 0
    assert "已取消" in out["reply"]
    with Session(engine) as db:
        assert count(db, RefundRequest) == 0
        assert get_confirm(db, token).status == "expired"


def test_timeout_on_confirm_rejects_without_write(client, engine, monkeypatch):
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    expire_clock(monkeypatch)

    resp = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": thread_id})
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "expired" and out["decision"] == "rejected"
    assert out.get("receipt") is None
    with Session(engine) as db:
        assert count(db, RefundRequest) == 0


def test_timeout_expires_at_boundary_is_rejected(client, engine, monkeypatch):
    """边界:now == expires_at 即视为超时(不允许"最后一毫秒"执行)。"""
    body = start_refund(client)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]

    from app.api import confirm as confirm_api

    with Session(engine) as db:
        card = get_confirm(db, token)
        expired_at = card.expires_at
    normalized = expired_at if expired_at.tzinfo else expired_at.replace(tzinfo=utc_now().tzinfo)
    monkeypatch.setattr(confirm_api, "_now", lambda: normalized)

    out = pending(client, thread_id)
    assert out["status"] == "expired"
    with Session(engine) as db:
        assert count(db, RefundRequest) == 0


# ---------------------------------------------------------------------------
# 5. 转人工确认卡(同一闭环,另一写工具)
# ---------------------------------------------------------------------------


def test_handoff_confirm_creates_ticket_with_verified_receipt(client, engine):
    body = post_chat(client, HANDOFF_TEXT, customer_id=CUSTOMER_HANDOFF)
    thread_id = body["session_id"]
    token = body["pending_confirm"]["confirm_token"]
    assert set(body["pending_confirm"]) >= {"what", "impact", "irreversibility", "alternatives", "confirm_token"}

    out = pending(client, thread_id)
    assert out["tool"] == "handoff__create_ticket"
    with Session(engine) as db:
        tickets_before = count(db, Ticket)

    resp = client.post(f"/api/c/confirm/{token}/confirm", json={"thread_id": thread_id})
    assert resp.status_code == 200, resp.text
    decision = resp.json()
    receipt = decision["receipt"]
    assert receipt["kind"] == "handoff"
    assert receipt["verified"] is True
    assert receipt["ticket_id"]
    assert "工单号" in decision["reply"]
    with Session(engine) as db:
        assert count(db, Ticket) == tickets_before + 1, "确认后恰好新增 1 张工单"
        ticket = db.get(Ticket, receipt["ticket_id"])
        assert ticket is not None
        assert ticket.status == "queued"
        assert ticket.handoff_reason
        assert ticket.summary
