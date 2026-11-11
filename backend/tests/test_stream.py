"""test_stream.py — W10-4 真流式验收(SSE 增量 / Fake 分块 / 聊天 delta / PII 跨块)。

覆盖:
1. ``RealLLMClient.stream_text`` 解析真实 SSE ``data:`` 增量与 ``[DONE]``;
2. ``RealLLMClient.stream_text`` 在 reply_text JSON 模式跨块抽取 ``"reply"``;
3. ``FakeLLM.stream_text`` 首块可先于整轮完成拿到,且拼接 == ``complete()`` 定稿;
4. ``/api/c/chat/stream`` 的 ``delta`` 帧全部先于 ``message`` / ``done``,拼接 == 定稿;
5. PII 跨块:未闭合 ``[`` 距末尾超过占位符长度时立即放行(ISS-66 不卡死);
6. PII 跨块:合法占位符被切成两块时不吐半个占位符;
7. 应用 lifespan 启停异步 worker(ISS-49)。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.main as main_module
from app.api import get_db
from app.graph import build as graph_build
from app.llm.base import STREAM_CHUNK_CHARS, SCHEMA_REPLY, FakeLLM, reply_text_from_raw
from app.llm.real_client import RealLLMClient
from app.main import app
from app.pii import MaskingLLM
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER = "seed-cust-001"
POLICY_TEXT = "退货的话,来回运费是谁出？"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


# ---------------------------------------------------------------------------
# fixtures(与 test_handoff_flow 同口径:真实 seed + 政策库 + lifespan)
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
# 1-2. RealLLMClient 真 SSE 解析
# ---------------------------------------------------------------------------


def _mock_stream_client(chunks: list[str], captured: dict) -> RealLLMClient:
    """构造一个 ``_http`` 被 MockTransport 替换的 RealLLMClient。"""
    body = "".join(
        "data: "
        + json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
        + "\n\n"
        for piece in chunks
    ) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    client = RealLLMClient(
        api_key="test-key", base_url="http://mock.local", model="mock-model"
    )
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_real_client_stream_parses_sse_deltas() -> None:
    """逐块增量按原始顺序吐出,请求体 stream=True,结束于 [DONE]。"""
    captured: dict = {}
    client = _mock_stream_client(["您", "好,", "请问订单号"], captured)
    try:
        pieces = list(client.stream_text([{"role": "user", "content": "测试"}]))
    finally:
        client._http.close()

    assert pieces == ["您", "好,", "请问订单号"]
    assert captured["body"].get("stream") is True


def test_real_client_stream_extracts_reply_json_across_chunks() -> None:
    """reply_text JSON 模式:``"reply"`` 跨事件块也能拼回完整正文。"""
    captured: dict = {}
    client = _mock_stream_client(['{"reply":"第一', '段内容"}'], captured)
    try:
        pieces = list(
            client.stream_text(
                [{"role": "user", "content": "测试"}], schema={"name": "reply_text"}
            )
        )
    finally:
        client._http.close()

    assert "".join(pieces) == "第一段内容"


# ---------------------------------------------------------------------------
# 3. FakeLLM 分块:首块先到,拼接与定稿一致
# ---------------------------------------------------------------------------


def test_fake_llm_stream_is_incremental_and_matches_complete() -> None:
    fake = FakeLLM()
    messages = [{"role": "user", "content": POLICY_TEXT}]
    whole = reply_text_from_raw(fake.complete(messages, SCHEMA_REPLY), SCHEMA_REPLY)
    assert len(whole) > STREAM_CHUNK_CHARS, "政策回复应长于一个分块,才能验证流式"

    gen = fake.stream_text(messages, SCHEMA_REPLY)
    first = next(gen)  # 不等整轮完成就能拿到首块
    rest = list(gen)

    assert first == whole[: len(first)]
    assert len(first) <= STREAM_CHUNK_CHARS
    assert "".join([first, *rest]) == whole
    assert len([first, *rest]) > 1


# ---------------------------------------------------------------------------
# 4. 聊天 SSE:delta 先于 message/done,拼接 == 定稿
# ---------------------------------------------------------------------------


def test_chat_stream_delta_frames_precede_done(client) -> None:
    resp = client.post(
        "/api/c/chat/stream",
        json={"merchant_id": MERCHANT_ID, "customer_id": CUSTOMER, "text": POLICY_TEXT},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = parse_sse(resp.text)
    names = [name for name, _ in frames]
    assert "delta" in names and "message" in names and "done" in names

    first_delta = names.index("delta")
    first_message = names.index("message")
    done_index = names.index("done")
    assert first_delta < first_message < done_index
    assert all(i < done_index for i, name in enumerate(names) if name == "delta")

    delta_text = "".join(data["delta"] for name, data in frames if name == "delta")
    message_text = "".join(data["delta"] for name, data in frames if name == "message")
    done = next(data for name, data in frames if name == "done")

    assert delta_text and delta_text == message_text == done["reply"]
    # 分块而非整段:首块短于完整回复
    first_piece = next(data["delta"] for name, data in frames if name == "delta")
    assert len(first_piece) < len(done["reply"])


# ---------------------------------------------------------------------------
# 5-6. PII 跨块缓冲防护(ISS-66)
# ---------------------------------------------------------------------------


class _StubStreamLLM:
    """最小 LLM 桩:只按给定分块吐出文本,不参与分类/编排。"""

    model_name = "stub-stream"

    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces

    def complete(self, messages, schema=None) -> str:  # noqa: ANN001 - 桩
        return "".join(self._pieces)

    def stream_text(self, messages, schema=None):  # noqa: ANN001 - 桩
        yield from self._pieces


def test_pii_stream_releases_far_unclosed_bracket() -> None:
    """未闭合 ``[`` 后已超占位符最大长度:立即放行,不等整段生成结束。"""
    long_tail = "[" + "这是一段很长的正文说明超过十六个字符" + "继续"
    stub = _StubStreamLLM(["退款说明" + long_tail, "正常结尾"])
    masking = MaskingLLM(stub)

    pieces = list(masking.stream_text([{"role": "user", "content": "查退款"}]))

    assert pieces, "未闭合 [ 不得把整段缓冲到生成结束"
    assert "[" in pieces[0] and pieces[0].endswith("继续")
    assert "".join(pieces) == "退款说明" + long_tail + "正常结尾"


def test_pii_stream_keeps_placeholder_intact_across_chunks() -> None:
    """合法占位符被切成两块:先吐占位符前的文本,占位符整体不拆。"""
    stub = _StubStreamLLM(["联系[PHONE", "_1] 请稍等"])
    masking = MaskingLLM(stub)

    pieces = list(masking.stream_text([{"role": "user", "content": "查订单"}]))

    assert pieces[0] == "联系", "未闭合的合法占位符前缀不应被吐出去"
    assert "".join(pieces) == "联系[PHONE_1] 请稍等"


# ---------------------------------------------------------------------------
# 7. lifespan 启停异步 worker(ISS-49)
# ---------------------------------------------------------------------------


def test_lifespan_starts_and_stops_job_worker(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def _start(engine):  # noqa: ANN001 - 桩
        calls.append(("start", engine))
        return object()

    def _stop(engine, timeout: float = 5.0) -> None:  # noqa: ANN001 - 桩
        calls.append(("stop", engine))

    monkeypatch.setattr(main_module.jobs, "start_worker", _start)
    monkeypatch.setattr(main_module.jobs, "stop_worker", _stop)

    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert calls and calls[0][0] == "start", "lifespan 进入时必须启动 worker"

    assert calls[-1][0] == "stop", "lifespan 退出时必须优雅停 worker"
    assert [name for name, _ in calls] == ["start", "stop"]
