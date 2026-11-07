"""W1-B API 层测试:TestClient 覆盖健康检查、订单/工单/退款 CRUD 与守卫式冲突。

用 dependency_overrides 把 get_db 指向测试 engine(不碰 dev.db)。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import get_db
from app.main import app
from app.models import Merchant, Order, Ticket


# ---------------------------------------------------------------------------
# 构造 helper
# ---------------------------------------------------------------------------

def _add_merchant(engine, mid: str = "m-1") -> None:
    with Session(engine) as s:
        s.add(Merchant(id=mid, name="测试店"))
        s.commit()


def _add_order(
    engine, oid: str = "o-1", status: str = "created", total: int = 10000
) -> None:
    with Session(engine) as s:
        s.add(
            Order(
                id=oid,
                merchant_id="m-1",
                customer_id="cust-1",
                status=status,
                total_cents=total,
                currency="CNY",
            )
        )
        s.commit()


def _add_ticket(
    engine, tid: str = "tk-1", status: str = "queued", claimed_by: str | None = None
) -> None:
    with Session(engine) as s:
        s.add(
            Ticket(
                id=tid,
                merchant_id="m-1",
                customer_id="cust-1",
                status=status,
                claimed_by=claimed_by,
            )
        )
        s.commit()


@pytest.fixture()
def client(engine):
    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    c = TestClient(app)
    yield c
    c.close()
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "healthy"}


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------

def test_create_order(client, engine):
    _add_merchant(engine)
    r = client.post(
        "/orders",
        json={"merchant_id": "m-1", "customer_id": "cust-1", "total_cents": 10000},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "created"
    assert r.json()["total_cents"] == 10000


def test_create_order_validation_error(client):
    r = client.post("/orders", json={"customer_id": "cust-1"})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_get_order(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "paid")
    r = client.get("/orders/o-1")
    assert r.status_code == 200
    assert r.json()["status"] == "paid"


def test_get_order_not_found(client):
    r = client.get("/orders/no-such")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_list_orders(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "created")
    _add_order(engine, "o-2", "paid")
    r = client.get("/orders")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_order_pay(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "created")
    r = client.post("/orders/o-1/pay")
    assert r.status_code == 200
    assert r.json()["status"] == "paid"
    assert r.json()["order_id"] == "o-1"


def test_order_pay_conflict(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "paid")
    r = client.post("/orders/o-1/pay")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "STATE_CONFLICT"
    assert body["error"]["param"] == "paid"


def test_order_ship(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "paid")
    assert client.post("/orders/o-1/ship").json()["status"] == "shipped"


def test_order_deliver(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "shipped")
    assert client.post("/orders/o-1/deliver").json()["status"] == "delivered"


# ---------------------------------------------------------------------------
# 工单
# ---------------------------------------------------------------------------

def test_create_ticket(client, engine):
    _add_merchant(engine)
    r = client.post(
        "/tickets", json={"merchant_id": "m-1", "customer_id": "cust-1"}
    )
    assert r.status_code == 201
    assert r.json()["status"] == "queued"


def test_ticket_claim(client, engine):
    _add_merchant(engine)
    _add_ticket(engine, "tk-1", "queued")
    r = client.post("/tickets/tk-1/claim", json={"service_id": "agent-1"})
    assert r.status_code == 200
    assert r.json()["status"] == "claimed"
    assert r.json()["ticket_id"] == "tk-1"


def test_ticket_claim_conflict(client, engine):
    _add_merchant(engine)
    _add_ticket(engine, "tk-1", "claimed", claimed_by="agent-1")
    r = client.post("/tickets/tk-1/claim", json={"service_id": "agent-2"})
    assert r.status_code == 409
    assert r.json()["error"]["param"] == "claimed"


def test_ticket_close(client, engine):
    _add_merchant(engine)
    _add_ticket(engine, "tk-1", "claimed", claimed_by="agent-1")
    assert client.post("/tickets/tk-1/close").json()["status"] == "closed"


def test_ticket_add_message(client, engine):
    _add_merchant(engine)
    _add_ticket(engine, "tk-1", "queued")
    r = client.post(
        "/tickets/tk-1/messages",
        json={"author_type": "user", "content": "你好", "is_internal": False},
    )
    assert r.status_code == 201
    assert r.json()["content"] == "你好"


def test_ticket_add_message_after_close(client, engine):
    _add_merchant(engine)
    _add_ticket(engine, "tk-1", "closed")
    r = client.post(
        "/tickets/tk-1/messages",
        json={"author_type": "user", "content": "补充", "is_internal": False},
    )
    assert r.status_code == 409
    assert r.json()["error"]["param"] == "closed"


def test_ticket_list_messages(client, engine):
    _add_merchant(engine)
    _add_ticket(engine, "tk-1", "queued")
    client.post(
        "/tickets/tk-1/messages",
        json={"author_type": "user", "content": "第一条", "is_internal": False},
    )
    r = client.get("/tickets/tk-1/messages")
    assert r.status_code == 200
    assert len(r.json()) == 1


# ---------------------------------------------------------------------------
# 退款
# ---------------------------------------------------------------------------

def _refund_payload(amount: int = 10000, key: str = "ik-1") -> dict:
    return {
        "merchant_id": "m-1",
        "order_id": "o-1",
        "amount_cents": amount,
        "reason": "质量问题",
        "idempotency_key": key,
    }


def test_refund_create(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "delivered", 10000)
    r = client.post("/refunds", json=_refund_payload())
    assert r.status_code == 201
    assert r.json()["status"] == "申请"
    assert r.json()["idempotent_hit"] is False


def test_refund_create_idempotent(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "delivered", 10000)
    r1 = client.post("/refunds", json=_refund_payload())
    r2 = client.post("/refunds", json=_refund_payload())
    assert r1.json()["refund_request_id"] == r2.json()["refund_request_id"]
    assert r2.json()["idempotent_hit"] is True


def test_refund_amount_exceeds_paid(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "paid", 10000)
    r = client.post("/refunds", json=_refund_payload(amount=10001))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "AMOUNT_EXCEEDS_PAID"


def test_refund_full_chain(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "delivered", 10000)
    r1 = client.post("/refunds", json=_refund_payload())
    rid = r1.json()["refund_request_id"]
    # 重复创建命中幂等
    r2 = client.post("/refunds", json=_refund_payload())
    assert r2.json()["idempotent_hit"] is True
    # 审核
    r3 = client.post(f"/refunds/{rid}/approve", json={"decided_by": "agent-1"})
    assert r3.json()["status"] == "审核"
    # 退款完成
    r4 = client.post(f"/refunds/{rid}/refund")
    assert r4.json()["status"] == "退款"
    # 回读订单
    assert client.get("/orders/o-1").json()["status"] == "refunded"


def test_refund_reject(client, engine):
    _add_merchant(engine)
    _add_order(engine, "o-1", "delivered", 10000)
    rid = client.post("/refunds", json=_refund_payload()).json()["refund_request_id"]
    r = client.post(f"/refunds/{rid}/reject", json={"decided_by": "agent-1"})
    assert r.json()["status"] == "驳回"
