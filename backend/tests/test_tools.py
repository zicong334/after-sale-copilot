"""W2-B 工具层测试:九字段契约 + 三层校验 + 失败四分类 + 幂等 + trace 事件。

覆盖任务书验收标准(≥20 个测试):
- schema 错回喂四要素齐全(SCHEMA_ERROR)
- 越权返回 NOT_FOUND_OR_FORBIDDEN 且不含订单任何字段
- 同幂等键调两次只有一张退款申请单
- refund 金额 > 实付被 BUSINESS_REJECT
- 写工具返回 pending_confirm 卡片含四要素 + confirm_token 落库
- 5 个工具九字段总表 dump
- tool_events 每次 invoke 一条事件、trace_id 贯穿

顶层 ``import app.tools`` 会触发 events.py 的 ToolEvent/ConfirmRequest 注册进
Base.metadata,保证 conftest 的 create_all 建出这两张表。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.guardrails import HANDOFF_REASON_ENUM, map_handoff_reason
from app.models import KnowledgeItem, Merchant, Order, RefundRequest, Ticket
from app.tools import (  # noqa: F401 —— 触发工具注册 + 事件表注册
    CODE_BUSINESS_REJECT,
    CODE_NOT_FOUND_OR_FORBIDDEN,
    CODE_SCHEMA_ERROR,
    ToolContext,
    dump_specs,
    get_spec,
    invoke,
    list_specs,
)
from app.tools.events import ConfirmRequest, ToolEvent
from app.tools.handoff import execute as handoff_execute
from app.tools.refund import compute_refund_key, execute as refund_execute
from scripts.seed import seed

# ---------------------------------------------------------------------------
# 构造 helper
# ---------------------------------------------------------------------------


def _merchant(session, mid: str = "m-1") -> Merchant:
    m = Merchant(id=mid, name="测试店")
    session.add(m)
    session.commit()
    return m


def _order(
    session,
    oid: str = "ORD-0001",
    customer_id: str = "cust-1",
    status: str = "created",
    total: int = 10000,
    mid: str = "m-1",
) -> Order:
    o = Order(
        id=oid,
        merchant_id=mid,
        customer_id=customer_id,
        status=status,
        total_cents=total,
        currency="CNY",
    )
    session.add(o)
    session.commit()
    return o


def _ctx(
    session,
    customer_id: str = "cust-1",
    trace_id: str | None = None,
    merchant_id: str = "m-1",
) -> ToolContext:
    return ToolContext(
        session=session,
        merchant_id=merchant_id,
        customer_id=customer_id,
        trace_id=trace_id or uuid.uuid4().hex,
    )


# ---------------------------------------------------------------------------
# 九字段契约 / 注册表
# ---------------------------------------------------------------------------

FIVE_TOOLS = {"shop__get_order", "policy__search", "policy__check_return",
              "refund__create", "handoff__create_ticket"}
NINE_FIELDS = {"name", "description", "input_schema", "output_schema",
               "side_effect", "timeout_s", "retry", "idempotency_key",
               "requires_confirm"}


def test_five_tools_registered():
    assert {s.name for s in list_specs()} == FIVE_TOOLS


def test_specs_nine_fields():
    for spec in list_specs():
        assert set(spec.to_dict().keys()) == NINE_FIELDS


def test_input_schema_additional_properties_false():
    for spec in list_specs():
        assert spec.input_schema["additionalProperties"] is False
        assert "required" in spec.input_schema


def test_order_id_pattern():
    """F1:order_id pattern 兼容 ORD-* 与真实 seed-order-* 两种格式。"""
    expected = r"^[A-Za-z0-9][A-Za-z0-9-]{2,63}$"
    for name in ("shop__get_order", "refund__create"):
        props = get_spec(name).input_schema["properties"]["order_id"]
        assert props["pattern"] == expected
    # handoff 的 order_id 可空,但 pattern 仍须一致(在 anyOf 的 string 分支)
    hprops = get_spec("handoff__create_ticket").input_schema["properties"]["order_id"]
    assert hprops["anyOf"][0]["pattern"] == expected
    assert {"type": "null"} in hprops["anyOf"]


def test_unknown_tool_not_found(session):
    _merchant(session)
    res = invoke("no__such_tool", {}, _ctx(session))
    assert res.ok is False
    assert res.error.code == CODE_NOT_FOUND_OR_FORBIDDEN


# ---------------------------------------------------------------------------
# shop__get_order
# ---------------------------------------------------------------------------

def test_shop_get_order_success(session):
    _merchant(session)
    _order(session, oid="ORD-0001", status="paid")
    res = invoke("shop__get_order", {"order_id": "ORD-0001"}, _ctx(session))
    assert res.ok is True
    assert res.data["order_id"] == "ORD-0001"
    assert res.data["status"] == "paid"
    assert res.data["total_cents"] == 10000


def test_shop_get_order_not_found(session):
    _merchant(session)
    res = invoke("shop__get_order", {"order_id": "ORD-NOPE"}, _ctx(session))
    assert res.ok is False
    assert res.error.code == CODE_NOT_FOUND_OR_FORBIDDEN
    assert res.error.param == "order_id"


def test_shop_get_order_forbidden_no_leak(session):
    """越权:订单属于 cust-A,当前会话 cust-B → NOT_FOUND_OR_FORBIDDEN,不泄露订单字段。"""
    _merchant(session)
    _order(session, oid="ORD-0001", customer_id="cust-A", status="paid", total=8888)
    res = invoke("shop__get_order", {"order_id": "ORD-0001"}, _ctx(session, customer_id="cust-B"))
    assert res.ok is False
    assert res.error.code == CODE_NOT_FOUND_OR_FORBIDDEN
    # 不含订单任何字段(订单号/状态/金额/币种/时间)
    body = res.to_dict()
    assert "ORD-0001" not in str(body["error"])
    assert "paid" not in str(body["error"])
    assert "8888" not in str(body["error"])
    assert "CNY" not in str(body["error"])


def test_shop_get_order_schema_error(session):
    _merchant(session)
    res = invoke("shop__get_order", {"order_id": "bad id"}, _ctx(session))
    assert res.ok is False
    assert res.error.code == CODE_SCHEMA_ERROR
    # 回喂四要素齐全
    err = res.error.to_dict()
    assert set(err.keys()) == {"code", "message", "retryable", "param"}
    assert err["retryable"] is False
    assert err["param"] == "order_id"


def test_shop_get_order_schema_missing_field(session):
    _merchant(session)
    res = invoke("shop__get_order", {}, _ctx(session))
    assert res.ok is False
    assert res.error.code == CODE_SCHEMA_ERROR


def test_shop_get_order_seed_order_real_customer(session):
    """F1:真实 seed 单号(seed-order-019,小写)通过 schema,归属客户可查到订单。"""
    seed(session)
    res = invoke(
        "shop__get_order",
        {"order_id": "seed-order-019"},
        _ctx(session, merchant_id="seed-merchant-1", customer_id="seed-cust-009"),
    )
    assert res.ok is True
    assert res.data["order_id"] == "seed-order-019"
    assert res.data["status"] == "delivered"


def test_shop_get_order_seed_order_non_owner_not_schema_error(session):
    """F1:非归属客户 → NOT_FOUND_OR_FORBIDDEN(证明进入 resolve 层,不是 schema 层拒绝)。"""
    seed(session)
    res = invoke(
        "shop__get_order",
        {"order_id": "seed-order-019"},
        _ctx(session, merchant_id="seed-merchant-1", customer_id="seed-cust-001"),
    )
    assert res.ok is False
    assert res.error.code == CODE_NOT_FOUND_OR_FORBIDDEN
    assert res.error.code != CODE_SCHEMA_ERROR


# ---------------------------------------------------------------------------
# policy__check_return
# ---------------------------------------------------------------------------

def test_check_return_eligible(session):
    _merchant(session)
    res = invoke(
        "policy__check_return",
        {"order_status": "delivered", "reason": "质量问题", "days_since_delivery": 3},
        _ctx(session),
    )
    assert res.ok is True
    assert res.data["eligible"] is True
    assert res.data["clause_id"] == "RP-03"


def test_check_return_overdue(session):
    _merchant(session)
    res = invoke(
        "policy__check_return",
        {"order_status": "delivered", "reason": "七天无理由", "days_since_delivery": 10},
        _ctx(session),
    )
    assert res.ok is True
    assert res.data["eligible"] is False
    assert res.data["clause_id"] == "RP-12"


def test_check_return_wrong_status(session):
    _merchant(session)
    res = invoke(
        "policy__check_return",
        {"order_status": "created", "reason": "质量问题", "days_since_delivery": 3},
        _ctx(session),
    )
    assert res.ok is True
    assert res.data["eligible"] is False


def test_check_return_schema_error(session):
    _merchant(session)
    res = invoke(
        "policy__check_return",
        {"order_status": "delivered", "reason": "不喜欢", "days_since_delivery": 3},
        _ctx(session),
    )
    assert res.ok is False
    assert res.error.code == CODE_SCHEMA_ERROR
    assert res.error.param == "reason"


# ---------------------------------------------------------------------------
# policy__search
# ---------------------------------------------------------------------------

def test_policy_search_no_hit_empty_kb(session):
    _merchant(session)
    res = invoke("policy__search", {"query": "七天无理由退货", "top_k": 5}, _ctx(session))
    assert res.ok is True
    assert res.data["no_hit"] is True
    assert res.data["results"] == []


def test_policy_search_hit(session):
    _merchant(session)
    session.add(KnowledgeItem(
        merchant_id="m-1",
        clause_id="RP-01",
        version="v1",
        title="七天无理由",
        content=(
            "七天无理由退换货 自签收之日起七天内 商品保持完好 不影响二次销售的 "
            "买家可申请无理由退货退款 运费由买家承担"
        ),
    ))
    session.commit()
    res = invoke("policy__search", {"query": "七天无理由退货", "top_k": 5}, _ctx(session))
    assert res.ok is True
    assert res.data["no_hit"] is False
    assert res.data["results"][0]["clause_id"] == "RP-01"


# ---------------------------------------------------------------------------
# refund__create
# ---------------------------------------------------------------------------

def _eligible_policy_check(ctx) -> None:
    """在同 trace 内落一条 eligible=True 的 policy__check_return 事件(FIX-2 前置)。"""
    res = invoke(
        "policy__check_return",
        {"order_status": "delivered", "reason": "质量问题", "days_since_delivery": 3},
        ctx,
    )
    assert res.ok is True
    assert res.data["eligible"] is True


def test_refund_pending_confirm_four_elements(session):
    """写工具 invoke 不执行,返回 pending_confirm 卡片含四要素 + confirm_token。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    ctx = _ctx(session)
    _eligible_policy_check(ctx)  # FIX-2:先有同 trace 的 eligible 核验
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "质量问题"},
        ctx,
    )
    assert res.ok is True
    card = res.data["pending_confirm"]
    for key in ("what", "impact", "irreversibility", "alternatives", "confirm_token"):
        assert key in card
        assert card[key]
    # 确认卡落库
    row = session.scalar(select(ConfirmRequest).where(
        ConfirmRequest.confirm_token == card["confirm_token"]
    ))
    assert row is not None
    assert row.status == "pending"
    # invoke 未真正执行:退款单仍为 0
    assert session.scalar(select(func.count()).select_from(RefundRequest)) == 0


def test_refund_requires_policy_gate(session):
    """FIX-2:无同 trace 的 policy__check_return 前置 → BUSINESS_REJECT 且零写。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "质量问题"},
        _ctx(session),
    )
    assert res.ok is False
    assert res.error.code == CODE_BUSINESS_REJECT
    assert res.error.retryable is False
    assert res.error.message == "需先核验退货资格"
    # 零写:既不出确认卡,也不建退款单
    assert session.scalar(select(func.count()).select_from(ConfirmRequest)) == 0
    assert session.scalar(select(func.count()).select_from(RefundRequest)) == 0


def test_refund_policy_gate_rejects_ineligible_check(session):
    """有前置但核验结果 eligible=False → 仍拒绝(不建单)。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    ctx = _ctx(session)
    check = invoke(
        "policy__check_return",
        {"order_status": "delivered", "reason": "七天无理由", "days_since_delivery": 10},
        ctx,
    )
    assert check.ok is True
    assert check.data["eligible"] is False
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "七天无理由"},
        ctx,
    )
    assert res.ok is False
    assert res.error.code == CODE_BUSINESS_REJECT
    assert session.scalar(select(func.count()).select_from(RefundRequest)) == 0


def test_refund_policy_gate_ignores_failed_check(session):
    """ok=False 的 policy__check_return 不算前置(入参 schema 错)。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    ctx = _ctx(session)
    bad = invoke(
        "policy__check_return",
        {"order_status": "delivered", "reason": "不存在的退款原因", "days_since_delivery": 3},
        ctx,
    )
    assert bad.ok is False
    assert bad.error.code == CODE_SCHEMA_ERROR
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "质量问题"},
        ctx,
    )
    assert res.ok is False
    assert res.error.code == CODE_BUSINESS_REJECT
    assert session.scalar(select(func.count()).select_from(RefundRequest)) == 0


def test_refund_policy_gate_bound_to_trace(session):
    """核验事件必须与退款调用同一 trace_id。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    _eligible_policy_check(_ctx(session, trace_id="trace-check"))
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "质量问题"},
        _ctx(session, trace_id="trace-refund"),
    )
    assert res.ok is False
    assert res.error.code == CODE_BUSINESS_REJECT


def test_refund_execute_idempotent_same_key(session):
    """同幂等键调两次只有一张退款申请单。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    args = {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "质量问题"}
    ctx = _ctx(session)
    r1 = refund_execute(args, ctx)
    assert r1["idempotent_hit"] is False
    r2 = refund_execute(args, ctx)
    assert r2["idempotent_hit"] is True
    assert r2["refund_id"] == r1["refund_id"]
    assert session.scalar(select(func.count()).select_from(RefundRequest)) == 1


def test_refund_key_stable(session):
    """幂等键 = sha256(order_id|refund_request|reason|amount_cents),可复算。"""
    _merchant(session)
    key1 = compute_refund_key("ORD-0001", "质量问题", 5000)
    key2 = compute_refund_key("ORD-0001", "质量问题", 5000)
    key3 = compute_refund_key("ORD-0001", "错发漏发", 5000)
    assert key1 == key2
    assert key1 != key3
    assert len(key1) == 64  # sha256 hex


def test_refund_amount_exceeds_paid_business_reject(session):
    """金额 > 实付 → BUSINESS_REJECT(不建单)。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 10001, "reason": "质量问题"},
        _ctx(session),
    )
    assert res.ok is False
    assert res.error.code == CODE_BUSINESS_REJECT
    assert res.error.retryable is False
    assert session.scalar(select(func.count()).select_from(RefundRequest)) == 0


def test_refund_order_not_found(session):
    _merchant(session)
    res = invoke(
        "refund__create",
        {"order_id": "ORD-NOPE", "amount_cents": 5000, "reason": "质量问题"},
        _ctx(session),
    )
    assert res.ok is False
    assert res.error.code == CODE_NOT_FOUND_OR_FORBIDDEN


def test_refund_forbidden(session):
    """越权:订单属于 cust-A,当前 cust-B → NOT_FOUND_OR_FORBIDDEN。"""
    _merchant(session)
    _order(session, oid="ORD-0001", customer_id="cust-A", status="delivered", total=10000)
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "质量问题"},
        _ctx(session, customer_id="cust-B"),
    )
    assert res.ok is False
    assert res.error.code == CODE_NOT_FOUND_OR_FORBIDDEN


def test_refund_duplicate_different_key(session):
    """同订单不同幂等键(不同原因)→ 业务拒绝,不建第二张单。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    ctx = _ctx(session)
    _eligible_policy_check(ctx)  # 先过 FIX-2 策略门,才能走到活跃退款单业务拒绝
    refund_execute({"order_id": "ORD-0001", "amount_cents": 5000, "reason": "质量问题"}, ctx)
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "错发漏发"},
        ctx,
    )
    assert res.ok is False
    assert res.error.code == CODE_BUSINESS_REJECT
    assert session.scalar(select(func.count()).select_from(RefundRequest)) == 1


def test_refund_schema_error(session):
    _merchant(session)
    res = invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 0, "reason": "质量问题"},
        _ctx(session),
    )
    assert res.ok is False
    assert res.error.code == CODE_SCHEMA_ERROR


# ---------------------------------------------------------------------------
# handoff__create_ticket
# ---------------------------------------------------------------------------

def test_handoff_pending_confirm(session):
    _merchant(session)
    res = invoke(
        "handoff__create_ticket",
        {"reason": "explicit_request", "summary": "用户多次尝试退货均失败"},
        _ctx(session),
    )
    assert res.ok is True
    card = res.data["pending_confirm"]
    for key in ("what", "impact", "irreversibility", "alternatives", "confirm_token"):
        assert key in card
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_handoff_execute_creates_ticket(session):
    _merchant(session)
    r = handoff_execute(
        {"reason": "unresolved_3rounds", "summary": "用户多次尝试退货均失败", "order_id": None},
        _ctx(session),
    )
    assert r["status"] == "queued"
    ticket = session.get(Ticket, r["ticket_id"])
    assert ticket is not None
    assert ticket.merchant_id == "m-1"
    assert ticket.customer_id == "cust-1"
    # F3 + FIX-6:handoff_reason 列存 canonical 枚举,summary 列存自然语言(分列落库)
    assert ticket.handoff_reason == "unresolved_3rounds"
    assert ticket.summary == "用户多次尝试退货均失败"


def test_handoff_order_not_found(session):
    _merchant(session)
    res = invoke(
        "handoff__create_ticket",
        {"reason": "explicit_request", "summary": "复杂问题", "order_id": "ORD-NOPE"},
        _ctx(session),
    )
    assert res.ok is False
    assert res.error.code == CODE_NOT_FOUND_OR_FORBIDDEN


def test_handoff_schema_error(session):
    _merchant(session)
    res = invoke(
        "handoff__create_ticket",
        {"reason": "", "summary": "摘要"},
        _ctx(session),
    )
    assert res.ok is False
    assert res.error.code == CODE_SCHEMA_ERROR


def test_handoff_invalid_reason_enum_rejected(session):
    """FIX-6:旧自然语言 reason → schema 层拒绝,不落工单。"""
    _merchant(session)
    res = invoke(
        "handoff__create_ticket",
        {"reason": "需要人工介入", "summary": "摘要"},
        _ctx(session),
    )
    assert res.ok is False
    assert res.error.code == CODE_SCHEMA_ERROR
    assert res.error.param == "reason"
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0


@pytest.mark.parametrize("reason", HANDOFF_REASON_ENUM)
def test_handoff_all_enum_reasons_accepted(session, reason):
    """七枚举值 invoke + execute 全通;handoff_reason 存枚举,summary 存自然语言。"""
    _merchant(session)
    ctx = _ctx(session)
    summary = "用户反馈问题,需人工跟进"
    res = invoke("handoff__create_ticket", {"reason": reason, "summary": summary}, ctx)
    assert res.ok is True
    assert res.data["pending_confirm"]["what"]
    r = handoff_execute({"reason": reason, "summary": summary}, ctx)
    ticket = session.get(Ticket, r["ticket_id"])
    assert ticket.handoff_reason == reason
    assert ticket.summary == summary


def test_handoff_input_schema_enum_matches_guardrails():
    """工具 schema 的 reason enum 与 guardrails.HANDOFF_REASON_ENUM 单一来源一致。"""
    schema = get_spec("handoff__create_ticket").input_schema
    enum = schema["properties"]["reason"]["enum"]
    assert tuple(enum) == HANDOFF_REASON_ENUM


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("step_limit", "step_limit"),
        ("连续 3 轮未解决", "unresolved_3rounds"),
        ("连续多轮未解决", "unresolved_3rounds"),
        ("明示人工", "explicit_request"),
        ("金额争议:客户声称金额与订单实付不符", "amount_dispute"),
        ("风险信号(投诉/金额争议)", "complaint"),
    ],
)
def test_trigger_reason_maps_to_canonical_enum(raw, expected):
    """图内/FakeLLM 触发点原串 → canonical 枚举(守护映射顺序:FIX-6)。"""
    assert map_handoff_reason(raw) == expected


# ---------------------------------------------------------------------------
# tool_events trace 贯穿
# ---------------------------------------------------------------------------

def test_tool_events_trace_id_span(session):
    """同 trace_id 下多次 invoke(含失败)各记一条事件,step 递增贯穿。"""
    _merchant(session)
    _order(session, oid="ORD-0001", status="delivered", total=10000)
    trace_id = "trace-demo-1"
    ctx = _ctx(session, trace_id=trace_id)

    invoke("shop__get_order", {"order_id": "ORD-0001"}, ctx)  # ok
    invoke(
        "policy__check_return",
        {"order_status": "delivered", "reason": "质量问题", "days_since_delivery": 3},
        ctx,
    )  # ok
    invoke("shop__get_order", {"order_id": "ORD-NOPE"}, ctx)  # fail
    invoke(
        "refund__create",
        {"order_id": "ORD-0001", "amount_cents": 5000, "reason": "质量问题"},
        ctx,
    )  # ok(pending_confirm)

    events = session.execute(
        select(ToolEvent).where(ToolEvent.trace_id == trace_id).order_by(ToolEvent.step)
    ).scalars().all()
    assert len(events) == 4
    assert [e.step for e in events] == [0, 1, 2, 3]
    assert all(e.trace_id == trace_id for e in events)
    # 失败事件 ok=False 且带 error_code
    fail = events[2]
    assert fail.ok is False
    assert fail.error_code == CODE_NOT_FOUND_OR_FORBIDDEN


def test_tool_events_schema_error_recorded(session):
    _merchant(session)
    trace_id = "trace-schema-1"
    res = invoke("shop__get_order", {"order_id": "bad id"}, _ctx(session, trace_id=trace_id))
    assert res.error.code == CODE_SCHEMA_ERROR
    ev = session.scalar(select(ToolEvent).where(ToolEvent.trace_id == trace_id))
    assert ev is not None
    assert ev.ok is False
    assert ev.error_code == CODE_SCHEMA_ERROR
