"""W4-C 真实客户端/裁判的**密封**单元测试。

约束:测试不读 .env、不构造网络连接、不调用真实 API。所有网络行为都由
``_StubClient`` 覆盖 ``_post`` 返回固定响应;key 使用明显的占位串,断言抓包与
异常信息中不出现该串。
"""
from __future__ import annotations

import json

import pytest

from app.eval.judge import LLMJudge
from app.llm import FakeLLM, build_llm, has_real_key
from app.llm.real_client import (
    LLMBatchAbort,
    LLMEmptyContentError,
    RealLLMClient,
    UsageStats,
)

_PLACEHOLDER_KEY = "unit-test-placeholder-not-a-real-key"


def _reply(content: str = "ok", *, prompt: int = 10, completion: int = 5) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


class _StubClient(RealLLMClient):
    """覆盖 _post:零网络;按脚本依次返回响应或抛异常。"""

    def __init__(self, script: list, **kwargs) -> None:
        super().__init__(api_key=_PLACEHOLDER_KEY, **kwargs)
        self.script = list(script)
        self.calls: list[dict] = []

    def _post(self, body: dict) -> dict:
        self.calls.append(json.loads(json.dumps(body)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """重试退避不真的睡,保持测试秒级。"""
    import app.llm.real_client as real_client

    monkeypatch.setattr(real_client.time, "sleep", lambda _seconds: None)


def test_build_llm_without_key_is_fake(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert has_real_key() is False
    assert isinstance(build_llm(), FakeLLM)


def test_build_llm_with_key_is_real(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", _PLACEHOLDER_KEY)
    monkeypatch.setenv("LLM_MODEL", "unit-test-model")
    client = build_llm(max_tokens=1024)
    try:
        assert isinstance(client, RealLLMClient)
        assert client.model_name == "unit-test-model"
    finally:
        client.close()


def test_empty_content_is_explicit_failure_and_budget_grows():
    stub = _StubClient(
        [
            _reply(""),
            _reply("   "),
            _reply(""),
        ],
        max_tokens=1024,
        max_retries=2,
    )
    with pytest.raises(LLMEmptyContentError):
        stub.complete([{"role": "user", "content": "hi"}])
    assert len(stub.calls) == 3
    budgets = [call["max_tokens"] for call in stub.calls]
    assert budgets == [1024, 2048, 4096]  # 空正文时翻倍预算再试,不静默成功
    assert stub.usage.empty_responses == 3
    assert stub.usage.prompt_tokens == 30
    stub.close()


def test_empty_then_success_returns_content():
    stub = _StubClient([_reply(""), _reply('{"reply": "您好"}')], max_retries=2)
    text = stub.complete([{"role": "user", "content": "hi"}])
    assert text == '{"reply": "您好"}'
    assert stub.usage.requests == 2
    stub.close()


def test_batch_abort_is_not_retried():
    stub = _StubClient([LLMBatchAbort("余额不足", status_code=402)], max_retries=2)
    with pytest.raises(LLMBatchAbort):
        stub.complete([{"role": "user", "content": "hi"}])
    assert len(stub.calls) == 1
    stub.close()


def test_5xx_aborts_batch_immediately_and_is_not_retried():
    """5xx 属批量错误:立即中止,不重试(烧钱纪律)。"""
    stub = _StubClient([LLMBatchAbort("上游 502", status_code=502)], max_retries=2)
    with pytest.raises(LLMBatchAbort):
        stub.complete([{"role": "user", "content": "hi"}])
    assert len(stub.calls) == 1
    stub.close()


def test_post_maps_5xx_to_batch_abort(monkeypatch):
    """_post 层把 5xx 直接映射为 LLMBatchAbort(不进入重试循环)。

    只替换 httpx.Client.post,不建立网络连接。
    """
    client = RealLLMClient(api_key=_PLACEHOLDER_KEY, max_retries=2)

    class _Resp:
        status_code = 503
        text = "upstream unavailable"

    monkeypatch.setattr(client._http, "post", lambda *args, **kwargs: _Resp())
    try:
        with pytest.raises(LLMBatchAbort) as exc_info:
            client._post({"model": "m", "messages": []})
        assert exc_info.value.status_code == 503
    finally:
        client.close()


def test_temperature_is_passed_on_complete_call():
    stub = _StubClient([_reply('{"reply": "ok"}')])
    stub.complete(
        [{"role": "user", "content": "hi"}],
        {"name": "reply_text", "type": "object"},
        temperature=0.1,
    )
    assert stub.calls[0]["temperature"] == 0.1
    stub.close()


def test_schema_call_requests_json_mode_and_strips_fences():
    stub = _StubClient([_reply('```json\n{"reply": "您好"}\n```')])
    text = stub.complete(
        [{"role": "user", "content": "hi"}],
        {"name": "reply_text", "type": "object"},
    )
    assert text == '{"reply": "您好"}'
    assert stub.calls[0]["response_format"] == {"type": "json_object"}
    assert stub.calls[0]["messages"][0]["role"] == "system"
    stub.close()


def test_usage_stats_delta_and_cost():
    before = UsageStats(prompt_tokens=10, completion_tokens=5, total_tokens=15, requests=1)
    after = UsageStats(prompt_tokens=30, completion_tokens=15, total_tokens=45, requests=3)
    delta = after - before
    assert delta.prompt_tokens == 20 and delta.requests == 2
    assert delta.cost_usd(1.0, 2.0) == pytest.approx(20 / 1e6 + 10 * 2 / 1e6)
    assert delta.cost_usd(None, 2.0) is None  # 单价未知 → TBD,不编造


def test_capture_file_has_no_key(tmp_dir):
    capture = tmp_dir / "capture.jsonl"
    stub = _StubClient([_reply('{"reply": "ok"}')], capture_path=capture)
    stub.complete([{"role": "user", "content": "你好"}], {"name": "reply_text"})
    text = capture.read_text(encoding="utf-8")
    record = json.loads(text.splitlines()[0])
    assert _PLACEHOLDER_KEY not in text
    assert "Authorization" not in text
    assert record["response"] == '{"reply": "ok"}'
    assert record["request"]["messages"]
    stub.close()


def test_llm_judge_passes_temperature_explicitly():
    class _JudgeClient:
        model_name = "unit-test-judge"

        def __init__(self) -> None:
            self.temperatures: list[float | None] = []

        def complete(self, messages, schema=None, *, temperature=None):
            self.temperatures.append(temperature)
            return json.dumps(
                {
                    "scores": {"accuracy": 90, "compliance": 90, "safety": 90},
                    "weighted_score": 90,
                    "severity": "none",
                    "critique": "ok",
                }
            )

    client = _JudgeClient()
    verdict = LLMJudge(client, temperature=0.1).judge(
        {"case_id": "CS-TEST", "user_input": "你好"},
        {"final_reply": "您好"},
    )
    assert client.temperatures == [0.1]  # W4-A 未尽事项④:显式传入
    assert verdict.passed is True
    assert verdict.scored_by == "llm"


def test_llm_judge_does_not_swallow_batch_abort():
    class _AbortClient:
        model_name = "unit-test-judge"

        def complete(self, messages, schema=None, *, temperature=None):
            raise LLMBatchAbort("key 失效", status_code=401)

    with pytest.raises(LLMBatchAbort):
        LLMJudge(_AbortClient()).judge(
            {"case_id": "CS-TEST", "user_input": "你好"},
            {"final_reply": "您好"},
        )


def test_llm_judge_falls_back_on_other_errors():
    class _BrokenClient:
        model_name = "unit-test-judge"

        def complete(self, messages, schema=None, *, temperature=None):
            raise RuntimeError("boom")

    verdict = LLMJudge(_BrokenClient()).judge(
        {"case_id": "CS-TEST", "user_input": "你好"},
        {"final_reply": "您好"},
    )
    assert verdict.fallback is True
    assert verdict.scored_by == "heuristic"
