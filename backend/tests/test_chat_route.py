"""会话薄路由 + MOCK 编排的端到端测试(W2-C 新增,≥10 条)。

覆盖任务书验收三场景:
1. 政策问答:带真实 clause_id 的 ``[n]`` 引用(引用必须存在于 knowledge_item);
2. 订单判责:符合→出退款确认卡(未确认不落 refund_request);超期→拒绝且零写操作;
   越权查单→NOT_FOUND_OR_FORBIDDEN 且不泄露字段;金额争议→转人工不下退款单;
3. 转人工:工单确认卡 + 会话 handed_off + 后续消息 AI 不再回复(入口拦截)。

数据用真实 seed() 与 data/policies/*.md(与 dev.db 同源),不造政策文本。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import get_db
from app.main import app
from app.models import ChatMessage, ChatSession, KnowledgeItem, RefundRequest
from app.orchestrator.w1_router import HANDOFF_BLOCKED_REPLY
from app.tools.events import ConfirmRequest, ToolEvent
from app.trace import EVENT_CLASSIFY, EVENT_QUERY_REWRITE, EVENT_ROUTE_BLOCKED, trace_events
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER_019 = "seed-cust-009"
CUSTOMER_020 = "seed-cust-010"
CUSTOMER_OTHER = "seed-cust-001"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(engine):
    """真实 seed(30 单/8 工单)+ 真实政策库(18 条 knowledge_item)。"""
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()
    yield


@pytest.fixture()
def client(engine, seeded):
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


def post_chat(
    client: TestClient,
    text: str,
    *,
    customer_id: str = CUSTOMER_OTHER,
    context: dict | None = None,
    session_id: str | None = None,
    attachments: list[str] | None = None,
):
    body: dict = {
        "merchant_id": MERCHANT_ID,
        "customer_id": customer_id,
        "text": text,
        "context": context or {},
    }
    if session_id:
        body["session_id"] = session_id
    if attachments is not None:
        body["attachments"] = attachments
    return client.post("/api/c/chat", json=body)


def tool_names(response) -> list[str]:
    return [call["name"] for call in response.json()["tool_calls"]]


def kb_clause_ids(engine) -> set[str]:
    with Session(engine) as session:
        rows = session.scalars(
            select(KnowledgeItem.clause_id).where(KnowledgeItem.clause_id.is_not(None))
        ).all()
    return set(rows)


def count_rows(engine, model) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def events_of(engine, trace_id: str) -> list[ToolEvent]:
    with Session(engine) as session:
        return trace_events(session, trace_id)


def messages_of(engine, session_id: str, trace_id: str | None = None) -> list[ChatMessage]:
    with Session(engine) as session:
        stmt = select(ChatMessage).where(ChatMessage.session_id == session_id)
        if trace_id:
            stmt = stmt.where(ChatMessage.trace_id == trace_id)
        return list(session.scalars(stmt))


# ---------------------------------------------------------------------------
# 场景一:政策问答
# ---------------------------------------------------------------------------


def test_policy_qa_returns_real_clause_citations(client, engine):
    """政策问答:回复带 [1] 引用,且 citations 的 clause_id 在知识库真实存在。"""
    r = post_chat(client, "衣服买大了想退，几天之内可以无理由退货？")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["intent"] == "policy_qa"
    assert "policy__search" in tool_names(r)
    assert data["citations"], "政策问答必须返回 citations 映射"
    known = kb_clause_ids(engine)
    for citation in data["citations"]:
        assert citation["clause_id"] in known
    assert "[1]" in data["reply"]
    assert data["citations"][0]["clause_id"] in data["reply"]
    assert "7" in data["reply"] and "无理由" in data["reply"]
    # 禁止话术:不得承诺"随时可退/无限期"
    for phrase in ("随时可退", "无限期"):
        assert phrase not in data["reply"]


def test_policy_qa_no_hit_triggers_deterministic_rewrite(client, engine):
    """检索无命中 → 确定性改写再查(MOCK),并写 query_rewrite trace 事件。"""
    r = post_chat(client, "退货的话，来回运费是谁出？")
    data = r.json()
    assert data["intent"] == "policy_qa"
    names = tool_names(r)
    assert names.count("policy__search") == 2, "首次无命中后应改写并二次检索"
    assert data["citations"], "改写后必须命中政策条款"
    assert "运费" in data["reply"] and "商家" in data["reply"]
    assert "全部由商家承担" not in data["reply"]
    rewrites = [e for e in events_of(engine, data["trace_id"]) if e.tool_name == EVENT_QUERY_REWRITE]
    assert len(rewrites) == 1
    assert json.loads(rewrites[0].payload)["rewritten"]


def test_policy_qa_size_question_rewrites_to_size_clause(client):
    """尺码问题(原句 BM25 无命中)→ 改写命中尺码条款,回复含"尺码"。"""
    r = post_chat(client, "这件 M 码适合多高多重的人穿？")
    data = r.json()
    assert data["intent"] == "policy_qa"
    assert tool_names(r).count("policy__search") == 2
    assert "尺码" in data["reply"]
    assert "保证合身" not in data["reply"]


# ---------------------------------------------------------------------------
# 场景二:订单判责
# ---------------------------------------------------------------------------


def test_order_judge_overdue_refuses_with_zero_write(client, engine):
    """9 天前的七天无理由退货:超期拒绝,引用 RP-12,零写操作。"""
    r = post_chat(
        client,
        "9 天前买的裙子还没穿，不喜欢了想退，能退吗？",
        customer_id=CUSTOMER_019,
        context={"order_id": "seed-order-019", "days_since_delivery": 9},
    )
    data = r.json()
    assert data["intent"] == "order_judge"
    names = tool_names(r)
    assert "shop__get_order" in names and "policy__check_return" in names
    assert "refund__create" not in names
    assert "7" in data["reply"]
    for phrase in ("可以退", "马上给您退款"):
        assert phrase not in data["reply"]
    assert data["citations"] and data["citations"][0]["clause_id"] == "RP-12"
    assert data["pending_confirm"] is None
    assert count_rows(engine, RefundRequest) == 0


def test_order_judge_eligible_creates_confirm_card_only(client, engine):
    """3 天前质量问题退货:符合 → 出退款确认卡,但未确认前不落 refund_request。"""
    r = post_chat(
        client,
        "3 天前收到的衣服有线头，是质量问题，我要退货退款",
        customer_id=CUSTOMER_020,
        context={"order_id": "seed-order-020", "days_since_delivery": 3},
    )
    data = r.json()
    names = tool_names(r)
    assert {"shop__get_order", "policy__check_return", "refund__create"} <= set(names)
    card = data["pending_confirm"]
    assert card is not None
    for key in ("what", "impact", "irreversibility", "alternatives", "confirm_token"):
        assert card.get(key), f"确认卡缺字段 {key}"
    assert data["citations"] and data["citations"][0]["clause_id"] == "RP-03"
    assert "15" in data["reply"] and "119" in data["reply"]
    # 关键:本棒只到"出确认卡",真正落库要等 W3 确认流 execute
    assert count_rows(engine, RefundRequest) == 0
    assert count_rows(engine, ConfirmRequest) == 1


def test_amount_dispute_goes_handoff_without_refund(client, engine):
    """金额争议(投诉 + 声称金额≠实付)→ 查单后转人工,绝不下退款单。"""
    r = post_chat(
        client,
        "我实付了 399，你们必须按 499 退给我，不然我投诉",
        customer_id=CUSTOMER_019,
        context={
            "order_id": "seed-order-019",
            "claimed_refund_cents": 49900,
            "claimed_paid_cents": 39900,
        },
    )
    data = r.json()
    names = tool_names(r)
    assert "shop__get_order" in names
    assert "handoff__create_ticket" in names
    assert "refund__create" not in names
    assert "按 499 退" not in data["reply"]
    assert data["pending_confirm"] is not None
    assert data["session_status"] == "handed_off"
    assert count_rows(engine, RefundRequest) == 0


def test_order_not_owned_does_not_leak_fields(client, engine):
    """越权查单:NOT_FOUND_OR_FORBIDDEN,回复不泄露地址/手机号/实付。"""
    r = post_chat(
        client,
        "帮我查一下订单 seed-order-019 的收货地址和买家手机号",
        customer_id=CUSTOMER_OTHER,
        context={"order_id": "seed-order-019", "current_customer_id": CUSTOMER_OTHER},
    )
    data = r.json()
    first_call = data["tool_calls"][0]
    assert first_call["name"] == "shop__get_order"
    assert first_call["ok"] is False
    assert first_call["error_code"] == "NOT_FOUND_OR_FORBIDDEN"
    for phrase in ("收货地址是", "手机号是", "实付"):
        assert phrase not in data["reply"]
    assert "refund__create" not in tool_names(r)
    assert data["session_status"] == "active"


# ---------------------------------------------------------------------------
# 场景三:转人工(含"转人工后 AI 不再回复")
# ---------------------------------------------------------------------------


def test_explicit_handoff_creates_ticket_and_stops_ai(client, engine):
    """明示转人工:快捷路径 conf=1.0 → 工单确认卡 + handed_off;再发消息被拦截。"""
    first = post_chat(client, "我不跟你聊了，直接给我转人工客服", customer_id=CUSTOMER_OTHER)
    data = first.json()
    assert data["intent"] == "handoff"
    assert data["classification_path"] == "fast_path"
    assert data["confidence"] == 1.0
    assert "handoff__create_ticket" in tool_names(first)
    assert data["pending_confirm"] is not None
    assert data["session_status"] == "handed_off"
    for phrase in ("我来帮您解决", "请稍等，我继续为您处理"):
        assert phrase not in data["reply"]

    session_id = data["session_id"]
    with Session(engine) as session:
        assert session.get(ChatSession, session_id).status == "handed_off"

    # 转人工后:入口守卫拦截,AI 不回复、不调编排、不产生任何工具调用
    second = post_chat(
        client, "我再补充一句:快递单号发你了", customer_id=CUSTOMER_OTHER, session_id=session_id
    )
    blocked = second.json()
    assert blocked["ai_replied"] is False
    assert blocked["intent"] == "blocked"
    assert blocked["reply"] == HANDOFF_BLOCKED_REPLY
    assert blocked["tool_calls"] == []
    assert blocked["session_status"] == "handed_off"
    events = events_of(engine, blocked["trace_id"])
    assert [e.tool_name for e in events] == [EVENT_ROUTE_BLOCKED]

    # 两轮消息都带同一个 trace_id 落库(第二轮 trace 只有拦截事件)
    msgs = messages_of(engine, session_id, trace_id=blocked["trace_id"])
    assert {m.role for m in msgs} == {"user", "assistant"}
    assert msgs[0].content == "我再补充一句:快递单号发你了"


def test_unresolved_rounds_handoff_trigger(client, engine):
    """连续 3 轮同意图未解决(context 标记)→ 转人工。"""
    r = post_chat(
        client,
        "还是没结果，帮我看看 seed-order-019 的退款",
        customer_id=CUSTOMER_019,
        context={"order_id": "seed-order-019", "unresolved_rounds": 3},
    )
    data = r.json()
    assert data["intent"] == "handoff"
    assert "连续" in data["intent_reason"]
    assert "handoff__create_ticket" in tool_names(r)
    assert data["session_status"] == "handed_off"


def test_three_same_intent_turns_escalate(client, engine):
    """真实历史滚动:同一会话连续 3 轮政策问答 → 第 3 轮自动升级转人工。"""
    first = post_chat(client, "无理由退货是几天？", customer_id=CUSTOMER_OTHER)
    assert first.json()["intent"] == "policy_qa"
    session_id = first.json()["session_id"]

    second = post_chat(
        client, "那质量问题几天内能退？", customer_id=CUSTOMER_OTHER, session_id=session_id
    )
    assert second.json()["intent"] == "policy_qa"

    third = post_chat(
        client, "运费到底谁承担？", customer_id=CUSTOMER_OTHER, session_id=session_id
    )
    data = third.json()
    assert data["intent"] == "handoff"
    assert data["classification_path"] == "rule:unresolved_3"
    assert "handoff__create_ticket" in tool_names(third)
    assert data["session_status"] == "handed_off"


# ---------------------------------------------------------------------------
# 路由与 trace 契约
# ---------------------------------------------------------------------------


def test_messages_and_events_share_trace_id(client, engine):
    """一次进线的 user/assistant 消息与全部 trace 事件共用一个 trace_id。"""
    r = post_chat(client, "无理由退货是几天？")
    data = r.json()
    trace_id = data["trace_id"]
    assert trace_id
    msgs = messages_of(engine, data["session_id"])
    assert {m.role for m in msgs} == {"user", "assistant"}
    assert all(m.trace_id == trace_id for m in msgs)
    events = events_of(engine, trace_id)
    assert {e.trace_id for e in events} == {trace_id}
    classify_events = [e for e in events if e.tool_name == EVENT_CLASSIFY]
    assert len(classify_events) == 1
    assert json.loads(classify_events[0].payload)["model"].endswith("(MOCK)")
    steps = [e.step for e in events]
    assert steps == sorted(steps) and len(steps) == len(set(steps))


def test_empty_text_returns_400(client):
    r = client.post(
        "/api/c/chat",
        json={"merchant_id": MERCHANT_ID, "customer_id": CUSTOMER_OTHER, "text": "  \u200b "},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["param"] == "text"


def test_attachment_base64_returns_400(client):
    r = post_chat(
        client,
        "破损照片见附件",
        attachments=["data:image/png;base64,AAAA"],
    )
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "attachments"


def test_session_belongs_to_other_customer_404(client):
    first = post_chat(client, "无理由退货是几天？", customer_id=CUSTOMER_OTHER)
    session_id = first.json()["session_id"]
    r = post_chat(
        client, "继续问", customer_id=CUSTOMER_019, session_id=session_id
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
