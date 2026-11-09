"""W7-1 PII 脱敏接线测试。

覆盖:
1. 手机号 / 身份证 / 地址三类各两个变体的 mask 与反解往返;
2. chat 路由接线:LLM 出网消息只见占位符,库里用户/助手消息与响应为原文;
3. 响应体顶层 ``pii_restore`` 能把占位符反解回原文;无 PII 时为 ``{}``。

说明:SpyLLM 的回复是固定 JSON(MOCK 输出),用于验证占位符反解,不代表真实模型。
本测试不发任何网络请求,数据用真实 ``seed()`` 与 ``data/policies/*.md``。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import chat as chat_api
from app.api import get_db
from app.graph import build as graph_build
from app.llm.base import SCHEMA_REPLY, FakeLLM, LLMClient, Message
from app.main import app
from app.models import ChatMessage
from app.pii import PIIMasker
from app.trace import trace_events
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER_OTHER = "seed-cust-001"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

# 三类 PII 各两个变体(手机号两个号段、身份证含 X 与纯数字、地址两个省市区)。
PHONE_A = "13812345678"
PHONE_B = "19987654321"
ID_CARD_A = "11010519491231002X"
ID_CARD_B = "330106199001011234"
ADDRESS_A = "浙江省杭州市西湖区文三路123号"
ADDRESS_B = "广东省深圳市南山区科技园南路8号"

PII_CASES = [
    (PHONE_A, "[PHONE_1]"),
    (PHONE_B, "[PHONE_1]"),
    (ID_CARD_A, "[ID_1]"),
    (ID_CARD_B, "[ID_1]"),
    (ADDRESS_A, "[ADDR_1]"),
    (ADDRESS_B, "[ADDR_1]"),
]


# ---------------------------------------------------------------------------
# fixtures(与 tests/test_chat_route.py 同源写法)
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
def client(engine, seeded, tmp_dir, monkeypatch):
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
# 工具函数与 SpyLLM(MOCK)
# ---------------------------------------------------------------------------


def post_chat(client: TestClient, text: str, *, customer_id: str = CUSTOMER_OTHER):
    body = {
        "merchant_id": MERCHANT_ID,
        "customer_id": customer_id,
        "text": text,
        "context": {},
    }
    return client.post("/api/c/chat", json=body)


def unmask(text: str, mapping: dict[str, str]) -> str:
    """按响应里的 pii_restore 映射把占位符换回原文(前端同款做法)。"""
    for placeholder, original in mapping.items():
        text = text.replace(placeholder, original)
    return text


class SpyLLM(LLMClient):
    """记录每次出网 messages 后委托 FakeLLM;回复固定 JSON(MOCK 输出)。

    - 意图分类等结构化调用原样委托 FakeLLM(规则实现,MOCK);
    - 回复调用(SCHEMA_REPLY)返回含占位符的固定文本,验证 chat 层反解。
    """

    #: MOCK:固定回复,故意只写占位符,由 MaskingLLM 反解成原文。
    MOCK_REPLY = (
        "已了解您的情况:手机号 [PHONE_1]、备用号码 [PHONE_2]、"
        "身份证 [ID_1]、地址 [ADDR_1],稍后为您核实。(MOCK)"
    )

    def __init__(self) -> None:
        self.inner = FakeLLM()
        self.calls: list[list[Message]] = []

    @property
    def model_name(self) -> str:
        return "spy-fake-llm (MOCK)"

    def complete(
        self, messages: list[Message], schema: dict | None = None
    ) -> str:
        self.calls.append(copy.deepcopy(messages))
        if schema is SCHEMA_REPLY:
            return json.dumps({"reply": self.MOCK_REPLY}, ensure_ascii=False)
        return self.inner.complete(messages, schema)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def captured_text(spy: SpyLLM) -> str:
    """把 SpyLLM 捕获的全部 messages 拼成一个字符串便于断言。"""
    return "\n".join(
        f"{message.get('role')}: {message.get('content')}"
        for call in spy.calls
        for message in call
    )


def events_of(engine, trace_id: str):
    with Session(engine) as session:
        return trace_events(session, trace_id)


# ---------------------------------------------------------------------------
# 1) PIIMasker 单元:三类 PII 各两变体 + 往返
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "placeholder"), PII_CASES)
def test_mask_each_pii_variant(raw: str, placeholder: str) -> None:
    """手机号/身份证/地址各变体都被替换成对应类型的占位符。"""
    masker = PIIMasker()
    masked = masker.mask(raw)
    assert masked == placeholder
    assert raw not in masked


def test_mask_restore_roundtrip() -> None:
    """mask → restore 往返必须等于原文,且同一原文复用同一占位符。"""
    masker = PIIMasker()
    text = (
        f"手机号{PHONE_A},备用{PHONE_B},身份证{ID_CARD_A}与{ID_CARD_B},"
        f"地址在{ADDRESS_A},另一个地址{ADDRESS_B}。"
    )
    masked = masker.mask(text)
    assert masker.restore(masked) == text
    # 同轮同类型按首现编号,不同原文不共用占位符
    assert "[PHONE_1]" in masked and "[PHONE_2]" in masked
    assert "[ID_1]" in masked and "[ID_2]" in masked
    assert "[ADDR_1]" in masked and "[ADDR_2]" in masked
    # 幂等:再次 mask 占位符文本不产生新映射
    assert masker.mask(masked) == masked
    assert len(masker.mapping) == 6


def test_mask_without_pii_is_unchanged() -> None:
    """无 PII 文本 mask 后完全相同,映射为空。"""
    masker = PIIMasker()
    text = "请问七天无理由退货怎么算?"
    assert masker.mask(text) == text
    assert masker.restore(text) == text
    assert masker.restore_mapping() == {}


# ---------------------------------------------------------------------------
# 2) chat 路由接线:出网只见占位符,库内/响应为原文
# ---------------------------------------------------------------------------


def test_chat_masks_outbound_llm_and_persists_original(client, engine, monkeypatch) -> None:
    spy = SpyLLM()
    monkeypatch.setattr(chat_api, "build_llm", lambda: spy)
    text = (
        f"我的手机号是{PHONE_A},备用{PHONE_B},身份证{ID_CARD_A},"
        f"地址在{ADDRESS_A},请问一般怎么处理比较好"
    )

    response = post_chat(client, text)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True

    # 至少调用了意图分类与回复两处,且出网内容不含任何原始 PII
    assert spy.calls, "LLM 未被调用,测试文本可能命中了快捷路径"
    outbound = captured_text(spy)
    for raw in (PHONE_A, PHONE_B, ID_CARD_A, ADDRESS_A):
        assert raw not in outbound
    assert "[PHONE_1]" in outbound and "[ID_1]" in outbound and "[ADDR_1]" in outbound

    # trace 事件 payload 同样只见占位符
    events = events_of(engine, body["trace_id"])
    assert events
    payload_text = json.dumps([event.payload for event in events], ensure_ascii=False)
    for raw in (PHONE_A, PHONE_B, ID_CARD_A, ADDRESS_A):
        assert raw not in payload_text

    # 库里 user/assistant 消息均为原文
    with Session(engine) as session:
        rows = session.scalars(
            select(ChatMessage).where(ChatMessage.trace_id == body["trace_id"])
        ).all()
    roles = {row.role: row for row in rows}
    assert roles["user"].content == text
    assert roles["assistant"].content
    for raw in (PHONE_A, PHONE_B, ID_CARD_A):
        assert raw in roles["assistant"].content
    assert "[PHONE_1]" not in roles["assistant"].content

    # 响应体顶层 pii_restore 能把回复里的占位符换回原文
    mapping = body["pii_restore"]
    assert isinstance(mapping, dict)
    assert mapping["[PHONE_1]"] == PHONE_A
    assert mapping["[PHONE_2]"] == PHONE_B
    assert mapping["[ID_1]"] == ID_CARD_A
    assert ADDRESS_A in mapping["[ADDR_1]"]
    assert unmask(body["reply"], mapping) == body["reply"]
    # 响应正文已是原文,不再含占位符
    assert "[PHONE_1]" not in body["reply"]


def test_chat_without_pii_restore_is_empty(client, engine) -> None:
    """无 PII 输入:mask 不改文本,响应 pii_restore 为 {},回复无占位符。"""
    text = "请问七天无理由退货的运费谁承担?"
    response = post_chat(client, text)

    assert response.status_code == 200
    body = response.json()
    assert body["pii_restore"] == {}
    assert "[PHONE_" not in body["reply"]
    assert body.get("degraded") is None

    with Session(engine) as session:
        roles = {
            row.role: row
            for row in session.scalars(
                select(ChatMessage).where(ChatMessage.trace_id == body["trace_id"])
            ).all()
        }
    assert roles["user"].content == text
