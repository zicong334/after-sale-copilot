"""W1-A 数据层模型测试:覆盖三铁律(金额整数分 / UTC 时间 / 状态 CHECK)、
三张核心表(orders / refund_request / ticket)约束、FK 级联、唯一约束、软删除。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import gen_id, utc_now
from app.models import (
    ChatMessage,
    ChatSession,
    EvalCase,
    KnowledgeItem,
    Merchant,
    Order,
    OrderItem,
    Product,
    ProductMetric,
    ProductVariant,
    RefundRequest,
    Review,
    Ticket,
    TicketMessage,
)


# ---------------------------------------------------------------------------
# 构造父对象的 helper(每个测试独立 session,先建最小合法父记录)
# ---------------------------------------------------------------------------

def _make_merchant(session, name: str = "测试店") -> Merchant:
    m = Merchant(name=name)
    session.add(m)
    session.commit()
    return m


def _make_product(session, merchant: Merchant, price_cents: int = 10000) -> Product:
    p = Product(merchant_id=merchant.id, title="测试商品", price_cents=price_cents)
    session.add(p)
    session.commit()
    return p


def _make_order(session, merchant: Merchant, status: str = "created",
                total_cents: int = 10000) -> Order:
    o = Order(merchant_id=merchant.id, customer_id="cust-1",
              total_cents=total_cents, status=status)
    session.add(o)
    session.commit()
    return o


def _make_chat_session(session, merchant: Merchant, status: str = "active") -> ChatSession:
    cs = ChatSession(merchant_id=merchant.id, customer_id="cust-1", status=status)
    session.add(cs)
    session.commit()
    return cs


def _make_ticket(session, merchant: Merchant, status: str = "queued",
                 claimed_by: str | None = None) -> Ticket:
    t = Ticket(merchant_id=merchant.id, customer_id="cust-1",
               status=status, claimed_by=claimed_by)
    session.add(t)
    session.commit()
    return t


def _make_refund(session, merchant: Merchant, order: Order,
                 amount_cents: int = 1000, reason: str = "质量问题",
                 status: str = "申请", idempotency_key: str | None = None,
                 decided_by: str | None = None) -> RefundRequest:
    r = RefundRequest(
        merchant_id=merchant.id,
        order_id=order.id,
        amount_cents=amount_cents,
        reason=reason,
        status=status,
        idempotency_key=idempotency_key or gen_id(),
        decided_by=decided_by,
    )
    session.add(r)
    session.commit()
    return r


def _make_eval_case(session, case_id: str = "CS-1", layer: str = "golden",
                    **overrides) -> EvalCase:
    data = dict(
        case_id=case_id,
        layer=layer,
        scene="退换货",
        user_input="上周买的裙子还没穿，能退吗？",
        context={"order_id": "ORD-10086", "days_since_delivery": 9},
        ground_truth={"eligible": False},
        expected_tools=["shop__get_order", "policy__check_return"],
        forbidden_tools=["refund__create"],
        expected_numbers=["7"],
        forbidden_phrases=["可以退"],
        must_ask_confirmation=False,
        expected_verdict={"task_success": True, "handoff": False},
        weight=1.0,
        repeat=3,
        rubric_version="cs-v1",
        source="prd.md#AC-07",
        notes="W1-A 模型测试样例",
    )
    data.update(overrides)
    e = EvalCase(**data)
    session.add(e)
    session.commit()
    return e


# ---------------------------------------------------------------------------
# merchant
# ---------------------------------------------------------------------------

def test_merchant_create(session):
    m = _make_merchant(session)
    assert m.id is not None
    assert m.name == "测试店"


def test_merchant_defaults(session):
    m = _make_merchant(session)
    assert m.platform == ""
    assert m.locale == "zh-CN"
    assert m.currency == "CNY"
    assert m.tz == "UTC"


# ---------------------------------------------------------------------------
# order(核心表一)
# ---------------------------------------------------------------------------

def test_order_create(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    assert o.status == "created"
    assert o.total_cents == 10000
    assert o.currency == "CNY"


def test_order_illegal_status_rejected(session):
    m = _make_merchant(session)
    o = Order(merchant_id=m.id, customer_id="cust-1", total_cents=100, status="illegal")
    session.add(o)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_order_negative_total_rejected(session):
    m = _make_merchant(session)
    o = Order(merchant_id=m.id, customer_id="cust-1", total_cents=-1)
    session.add(o)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_order_delete_merchant_cascade(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    oid = o.id
    session.delete(m)
    session.commit()
    # 新 Session 查询,绕过旧 Session 的 identity map(旧对象已因 DB 级联删除而失效)
    with Session(session.bind) as s2:
        assert s2.get(Order, oid) is None


# ---------------------------------------------------------------------------
# refund_request(核心表二)
# ---------------------------------------------------------------------------

def test_refund_create(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    r = _make_refund(session, m, o)
    assert r.status == "申请"
    assert r.amount_cents == 1000
    assert r.idempotency_key is not None


def test_refund_duplicate_idempotency_rejected(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    _make_refund(session, m, o, idempotency_key="k-dup")
    r2 = RefundRequest(merchant_id=m.id, order_id=o.id, amount_cents=100,
                       reason="质量问题", idempotency_key="k-dup")
    session.add(r2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_refund_second_active_rejected(session):
    """同订单两张进行中(申请/审核/退款)退款单,触发部分唯一索引 uq_refund_active。"""
    m = _make_merchant(session)
    o = _make_order(session, m)
    _make_refund(session, m, o, idempotency_key="k1")
    r2 = RefundRequest(merchant_id=m.id, order_id=o.id, amount_cents=100,
                       reason="质量问题", idempotency_key="k2")
    session.add(r2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_refund_negative_amount_rejected(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    r = RefundRequest(merchant_id=m.id, order_id=o.id, amount_cents=-1,
                      reason="质量问题", idempotency_key="k-neg")
    session.add(r)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_refund_illegal_status_rejected(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    r = RefundRequest(merchant_id=m.id, order_id=o.id, amount_cents=100,
                      reason="质量问题", status="非法", idempotency_key="k-s")
    session.add(r)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_refund_illegal_reason_rejected(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    r = RefundRequest(merchant_id=m.id, order_id=o.id, amount_cents=100,
                      reason="其他原因", idempotency_key="k-r")
    session.add(r)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_refund_invariant_apply_requires_no_decided_by(session):
    """不变量:status='申请' 时必须 decided_by IS NULL。"""
    m = _make_merchant(session)
    o = _make_order(session, m)
    r = RefundRequest(merchant_id=m.id, order_id=o.id, amount_cents=100,
                      reason="质量问题", status="申请",
                      idempotency_key="k-inv1", decided_by="agent-1")
    session.add(r)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_refund_invariant_review_requires_decided_by(session):
    """不变量:status='审核'(非申请)时必须 decided_by 非空。"""
    m = _make_merchant(session)
    o = _make_order(session, m)
    r = RefundRequest(merchant_id=m.id, order_id=o.id, amount_cents=100,
                      reason="质量问题", status="审核",
                      idempotency_key="k-inv2", decided_by=None)
    session.add(r)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# ticket(核心表三)
# ---------------------------------------------------------------------------

def test_ticket_create(session):
    m = _make_merchant(session)
    t = _make_ticket(session, m)
    assert t.status == "queued"
    assert t.claimed_by is None


def test_ticket_illegal_status_rejected(session):
    m = _make_merchant(session)
    t = Ticket(merchant_id=m.id, customer_id="cust-1", status="illegal")
    session.add(t)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_ticket_queued_with_claimed_by_rejected(session):
    """不变量:status='queued' 时 claimed_by 必须为空。"""
    m = _make_merchant(session)
    t = Ticket(merchant_id=m.id, customer_id="cust-1",
               status="queued", claimed_by="agent-1")
    session.add(t)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_ticket_claimed_without_claimed_by_rejected(session):
    """不变量:status='claimed'(非 queued 非 closed)时 claimed_by 必须非空。"""
    m = _make_merchant(session)
    t = Ticket(merchant_id=m.id, customer_id="cust-1",
               status="claimed", claimed_by=None)
    session.add(t)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_ticket_closed_any_claimed_by_ok(session):
    """status='closed' 时 claimed_by 任意(空或非空)均合法。"""
    m = _make_merchant(session)
    t = _make_ticket(session, m, status="closed", claimed_by=None)
    assert t.status == "closed"
    assert session.get(Ticket, t.id) is not None


# ---------------------------------------------------------------------------
# chat_session / chat_message(核心表四)
# ---------------------------------------------------------------------------

def test_chat_session_create(session):
    m = _make_merchant(session)
    cs = _make_chat_session(session, m)
    assert cs.status == "active"


def test_chat_session_illegal_status_rejected(session):
    m = _make_merchant(session)
    cs = ChatSession(merchant_id=m.id, customer_id="cust-1", status="illegal")
    session.add(cs)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_chat_session_soft_delete(session):
    m = _make_merchant(session)
    cs = _make_chat_session(session, m)
    cs.deleted_at = utc_now()
    session.commit()
    fetched = session.get(ChatSession, cs.id)
    assert fetched is not None
    assert fetched.deleted_at is not None


def test_chat_message_illegal_role_rejected(session):
    m = _make_merchant(session)
    cs = _make_chat_session(session, m)
    cm = ChatMessage(session_id=cs.id, role="illegal", content="你好")
    session.add(cm)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# catalog:product_variant / review / product_metric
# ---------------------------------------------------------------------------

def test_product_variant_negative_stock_rejected(session):
    m = _make_merchant(session)
    p = _make_product(session, m)
    pv = ProductVariant(product_id=p.id, sku="SKU-1", stock=-1)
    session.add(pv)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_product_variant_sku_unique(session):
    m = _make_merchant(session)
    p = _make_product(session, m)
    pv1 = ProductVariant(product_id=p.id, sku="SKU-DUP")
    session.add(pv1)
    session.commit()
    pv2 = ProductVariant(product_id=p.id, sku="SKU-DUP")
    session.add(pv2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_review_rating_out_of_range_rejected(session):
    m = _make_merchant(session)
    p = _make_product(session, m)
    rv = Review(merchant_id=m.id, product_id=p.id, rating=6)
    session.add(rv)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_review_illegal_sentiment_rejected(session):
    m = _make_merchant(session)
    p = _make_product(session, m)
    rv = Review(merchant_id=m.id, product_id=p.id, rating=5, sentiment="angry")
    session.add(rv)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_product_metric_composite_pk_unique(session):
    m = _make_merchant(session)
    p = _make_product(session, m)
    # 用 Core insert 直落 SQL,专门验证 DB 层复合主键唯一约束(不走 ORM identity map)
    session.execute(insert(ProductMetric).values(
        product_id=p.id, date=date(2026, 1, 1),
        views=0, orders_count=0, revenue_cents=0,
    ))
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(insert(ProductMetric).values(
            product_id=p.id, date=date(2026, 1, 1),
            views=0, orders_count=0, revenue_cents=0,
        ))
    session.rollback()


# ---------------------------------------------------------------------------
# order_item
# ---------------------------------------------------------------------------

def test_order_item_qty_zero_rejected(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    oi = OrderItem(order_id=o.id, qty=0, unit_price_cents=100)
    session.add(oi)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_order_item_delete_order_cascade(session):
    m = _make_merchant(session)
    o = _make_order(session, m)
    oi = OrderItem(order_id=o.id, qty=1, unit_price_cents=100)
    session.add(oi)
    session.commit()
    oi_id = oi.id
    session.delete(o)
    session.commit()
    # 新 Session 查询,绕过旧 Session 的 identity map
    with Session(session.bind) as s2:
        assert s2.get(OrderItem, oi_id) is None


# ---------------------------------------------------------------------------
# ticket_message
# ---------------------------------------------------------------------------

def test_ticket_message_illegal_author_type_rejected(session):
    m = _make_merchant(session)
    t = _make_ticket(session, m)
    tm = TicketMessage(ticket_id=t.id, author_type="bot", content="x")
    session.add(tm)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# knowledge_item(软删除)
# ---------------------------------------------------------------------------

def test_knowledge_item_soft_delete(session):
    m = _make_merchant(session)
    ki = KnowledgeItem(merchant_id=m.id, content="七天无理由退货政策")
    session.add(ki)
    session.commit()
    ki.deleted_at = utc_now()
    session.commit()
    assert session.get(KnowledgeItem, ki.id) is not None


# ---------------------------------------------------------------------------
# eval_case(17 字段 + 唯一 + layer CHECK)
# ---------------------------------------------------------------------------

def test_eval_case_create_17_fields(session):
    e = _make_eval_case(session)
    assert e.case_id == "CS-1"
    assert e.layer == "golden"
    assert e.expected_tools == ["shop__get_order", "policy__check_return"]
    assert e.forbidden_phrases == ["可以退"]
    assert e.repeat == 3
    assert e.must_ask_confirmation is False
    assert e.deleted_at is None


def test_eval_case_case_id_unique(session):
    _make_eval_case(session, case_id="CS-DUP")
    e2 = EvalCase(case_id="CS-DUP", layer="golden", scene="s",
                  user_input="u", context={}, ground_truth={},
                  expected_tools=[], forbidden_tools=[], expected_numbers=[],
                  forbidden_phrases=[], must_ask_confirmation=False,
                  expected_verdict={}, weight=1.0, repeat=1,
                  rubric_version="v1", source="s", notes="n")
    session.add(e2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_eval_case_illegal_layer_rejected(session):
    e = EvalCase(
        case_id="CS-LAYER", layer="illegal", scene="s",
        user_input="u", context={}, ground_truth={},
        expected_tools=[], forbidden_tools=[], expected_numbers=[],
        forbidden_phrases=[], must_ask_confirmation=False,
        expected_verdict={}, weight=1.0, repeat=1,
        rubric_version="v1", source="s", notes="n",
    )
    session.add(e)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# 三铁律:UTC 时间 + 外键强制
# ---------------------------------------------------------------------------

def test_utc_now_is_tz_aware(session):
    now = utc_now()
    assert now.tzinfo is not None


def test_fk_merchant_enforced(session):
    o = Order(merchant_id="nonexistent-merchant", customer_id="cust-1", total_cents=100)
    session.add(o)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
