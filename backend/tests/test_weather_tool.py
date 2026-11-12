"""test_weather_tool.py — W16 天气查询工具的验收测试(工具层 + 图级 + 越界回归)。

硬约束:
- pytest/CI 内**零真实外呼**:所有 HTTP 都 MOCK 掉 ``urllib.request.urlopen``;
- 不调真实 LLM:图级用例走 ``FakeLLM`` 规则;
- ``data/eval/`` 零改动:评测集不在本文件读写。

覆盖任务书要求:
1. 工具层:geocode 成功 / 城市不存在 / 超时 / HTTP 错误 → 结构化错误信封
   ``WEATHER_UPSTREAM_ERROR``;TTL 缓存命中不二次请求;城市名经 urlencode 转义;
   输出字段完整;失败也留 tool_events 痕迹(可观测自动落表)。
2. 图级:FakeLLM 天气消息 → act 调 ``weather__query`` → compose 附固定来源口径;
   「暴雨导致快递没到」+ 已锁订单 → 履约链不丢,天气只作附注。
3. 越界回归:投资/菜谱等仍 handoff(天气白名单不得削弱既有越界拦截)。
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.graph import runner
from app.models import ChatSession
from app.tools import weather as weather_tool
from app.tools.events import ToolEvent
from app.tools.registry import invoke
from app.tools.results import ToolContext
from app.trace import new_trace_id
from scripts.load_policies import parse_policies, upsert_policies
from scripts.seed import seed

MERCHANT_ID = "seed-merchant-1"
CUSTOMER_020 = "seed-cust-010"
POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

#: MOCK 上游响应(不产生任何真实网络请求)。
GEOCODE_OK: dict = {
    "results": [{"name": "杭州", "latitude": 30.2936, "longitude": 120.1614}]
}
FORECAST_OK: dict = {
    "current_weather": {
        "temperature": 21.4,
        "apparent_temperature": 20.1,
        "weathercode": 82,
        "time": "2026-11-12T10:00",
    },
    "daily": {
        "temperature_2m_max": [23.1],
        "temperature_2m_min": [17.2],
        "precipitation_probability_max": [80],
    },
}


class _FakeResponse:
    """MOCK:假的 HTTP 响应体。"""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeHTTP:
    """MOCK:假的 ``urlopen``——按 URL 里的域名分派 MOCK 响应,并记录调用。"""

    def __init__(
        self,
        geocode: dict | None = None,
        forecast: dict | None = None,
        error: Exception | None = None,
    ) -> None:
        self.geocode = GEOCODE_OK if geocode is None else geocode
        self.forecast = FORECAST_OK if forecast is None else forecast
        self.error = error
        self.calls: list[str] = []

    def __call__(self, request: object, timeout: float | None = None) -> _FakeResponse:
        url = str(getattr(request, "full_url", request))
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        payload = self.geocode if "geocoding-api" in url else self.forecast
        return _FakeResponse(payload)


@pytest.fixture(autouse=True)
def _clean_weather_cache():
    """天气缓存是进程级的:用例之间必须清空,避免互相命中。"""
    weather_tool._CACHE.clear()
    yield
    weather_tool._CACHE.clear()


@pytest.fixture()
def http(monkeypatch) -> _FakeHTTP:
    fake = _FakeHTTP()
    monkeypatch.setattr(weather_tool.urllib.request, "urlopen", fake)
    return fake


@pytest.fixture()
def ctx(session) -> ToolContext:
    return ToolContext(
        session=session,
        merchant_id=MERCHANT_ID,
        customer_id=CUSTOMER_020,
        trace_id="trace-w16-weather",
        session_id="sess-w16-weather",
    )


# ---------------------------------------------------------------------------
# 1. 工具层
# ---------------------------------------------------------------------------


def test_weather_query_returns_full_payload(http, ctx):
    result = invoke("weather__query", {"city": "杭州"}, ctx)

    assert result.ok is True
    data = result.data
    assert data["city"] == "杭州"
    assert data["temperature"] == 21.4
    assert data["apparent_temperature"] == 20.1
    assert data["weather"] == "暴雨"
    assert data["weather_code"] == 82
    assert data["temp_max"] == 23.1
    assert data["temp_min"] == 17.2
    assert data["precipitation_probability"] == 80
    assert data["observed_at"]
    assert data["source"] == "Open-Meteo"


def test_weather_query_hits_ttl_cache_without_second_request(http, ctx):
    first = invoke("weather__query", {"city": "杭州"}, ctx)
    second = invoke("weather__query", {"city": "杭州"}, ctx)

    assert first.ok is True and second.ok is True
    assert second.data == first.data
    assert len(http.calls) == 2, "10 分钟 TTL 内同城市只应外呼一次(geocode+forecast 各一次)"


def test_weather_query_cache_expires(monkeypatch, http, ctx):
    monkeypatch.setattr(weather_tool, "CACHE_TTL_S", 0.0)

    invoke("weather__query", {"city": "杭州"}, ctx)
    invoke("weather__query", {"city": "杭州"}, ctx)

    assert len(http.calls) == 4, "TTL 过期后必须重新取数"


def test_weather_city_not_found_is_structured_upstream_error(monkeypatch, ctx):
    fake = _FakeHTTP(geocode={"results": []})
    monkeypatch.setattr(weather_tool.urllib.request, "urlopen", fake)

    result = invoke("weather__query", {"city": "不存在城"}, ctx)

    assert result.ok is False
    assert result.error.code == "WEATHER_UPSTREAM_ERROR"
    assert result.error.retryable is False
    assert result.error.param == "city"


def test_weather_timeout_is_structured_upstream_error(monkeypatch, ctx):
    fake = _FakeHTTP(error=TimeoutError("timed out"))
    monkeypatch.setattr(weather_tool.urllib.request, "urlopen", fake)

    result = invoke("weather__query", {"city": "杭州"}, ctx)

    assert result.ok is False
    assert result.error.code == "WEATHER_UPSTREAM_ERROR"
    assert result.error.retryable is True


def test_weather_upstream_http_error_is_structured_upstream_error(monkeypatch, ctx):
    fake = _FakeHTTP(error=urllib.error.URLError("404 Not Found"))
    monkeypatch.setattr(weather_tool.urllib.request, "urlopen", fake)

    result = invoke("weather__query", {"city": "杭州"}, ctx)

    assert result.ok is False
    assert result.error.code == "WEATHER_UPSTREAM_ERROR"


def test_weather_city_is_url_encoded(monkeypatch, ctx):
    fake = _FakeHTTP()
    monkeypatch.setattr(weather_tool.urllib.request, "urlopen", fake)

    result = invoke("weather__query", {"city": "杭州&x"}, ctx)

    assert result.ok is True
    geocode_url = [url for url in fake.calls if "geocoding-api" in url][0]
    assert "name=%E6%9D%AD%E5%B7%9E%26x" in geocode_url, "城市名必须经 urlencode 转义"
    assert "&x" not in geocode_url


def test_weather_rejects_illegal_city_without_calling_upstream(monkeypatch, ctx):
    fake = _FakeHTTP()
    monkeypatch.setattr(weather_tool.urllib.request, "urlopen", fake)

    result = invoke("weather__query", {"city": "杭州/../etc"}, ctx)

    assert result.ok is False
    assert result.error.code == "SCHEMA_ERROR"
    assert fake.calls == [], "参数不合法时不得外呼"


def test_weather_failure_still_writes_tool_event(monkeypatch, session, ctx):
    fake = _FakeHTTP(error=TimeoutError("timed out"))
    monkeypatch.setattr(weather_tool.urllib.request, "urlopen", fake)

    invoke("weather__query", {"city": "杭州"}, ctx)
    session.expire_all()
    rows = (
        session.query(ToolEvent)
        .filter(ToolEvent.tool_name == "weather__query")
        .all()
    )

    assert rows, "工具调用必须落 tool_events(可观测无需另改)"
    assert rows[-1].error_code == "WEATHER_UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# 2. 图级(FakeLLM,零真实外呼)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(engine):
    with Session(engine) as session:
        seed(session)
        upsert_policies(session, parse_policies(POLICY_DIR))
        session.commit()
        yield session


@pytest.fixture()
def ckpt(tmp_dir) -> Path:
    return tmp_dir / "checkpoints.db"


def new_row(db: Session, customer_id: str = CUSTOMER_020) -> ChatSession:
    row = ChatSession(merchant_id=MERCHANT_ID, customer_id=customer_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def run_turn(db: Session, row: ChatSession, text: str, *, ckpt: Path, context=None):
    return runner.run(
        row.id,
        text,
        session=db,
        context=context,
        trace_id=new_trace_id(),
        checkpoint_path=ckpt,
    )


def tool_names(result) -> list[str]:
    return [call["name"] for call in result.tool_calls]


def test_weather_turn_calls_tool_and_appends_source_note(db, ckpt, http):
    row = new_row(db)

    result = run_turn(
        db, row, "杭州今天天气怎么样？会不会影响快递配送？", ckpt=ckpt
    )

    assert result.intent == "weather"
    assert tool_names(result) == ["weather__query"]
    assert "Open-Meteo" in result.reply
    assert "仅供参考" in result.reply
    assert "物流时效以承运方为准" in result.reply
    assert "暴雨" in result.reply


def test_weather_note_does_not_break_order_chain(db, ckpt, http):
    """履约链优先:已锁订单 + 延误语境 → 先答售后处置,天气只作附注。"""
    row = new_row(db)

    result = run_turn(
        db,
        row,
        "武汉暴雨导致我的快递没到，订单一直没更新",
        ckpt=ckpt,
        context={"order_id": "seed-order-020", "days_since_delivery": 3},
    )

    assert result.intent == "order_judge"
    names = tool_names(result)
    assert "shop__get_order" in names, "订单核验链不能被天气挤掉"
    assert "weather__query" in names
    assert "物流时效以承运方为准" in result.reply
    assert "seed-order-020" in result.reply or "订单" in result.reply


def test_weather_missing_city_asks_for_city(db, ckpt, http):
    row = new_row(db)

    result = run_turn(
        db, row, "今天天气怎么样？会不会影响我的配送？", ckpt=ckpt
    )

    assert result.intent == "weather"
    assert tool_names(result) == [], "抽不到城市时不臆造天气,也不白跑工具"
    assert "城市" in result.reply
    assert "物流时效以承运方为准" in result.reply


def test_weather_upstream_failure_does_not_break_the_reply(db, ckpt, monkeypatch):
    fake = _FakeHTTP(error=TimeoutError("timed out"))
    monkeypatch.setattr(weather_tool.urllib.request, "urlopen", fake)
    row = new_row(db)

    result = run_turn(
        db, row, "杭州今天天气怎么样？会不会影响快递配送？", ckpt=ckpt
    )

    assert result.intent == "weather"
    assert tool_names(result) == ["weather__query"]
    assert result.reply, "上游失败也要给出可读回复,不能炸图"
    assert "物流时效以承运方为准" in result.reply


# ---------------------------------------------------------------------------
# 3. 越界回归(天气白名单不得削弱既有越界拦截)
# ---------------------------------------------------------------------------


def test_out_of_scope_investment_still_handoff(db, ckpt, http):
    row = new_row(db)

    result = run_turn(db, row, "帮我推荐几只基金，哪只涨得好？", ckpt=ckpt)

    assert result.intent == "handoff"
    assert "weather__query" not in tool_names(result)
    assert result.pending_confirm is not None, "越界主题仍走人工确认卡"


def test_out_of_scope_recipe_still_handoff(db, ckpt, http):
    row = new_row(db)

    result = run_turn(db, row, "推荐个菜谱，今晚想做饭", ckpt=ckpt)

    assert result.intent == "handoff"
    assert "weather__query" not in tool_names(result)


def test_plain_smalltalk_is_not_treated_as_weather(db, ckpt, http):
    """弱档天气词必须叠加物流语境:闲聊天气不进天气工具。"""
    row = new_row(db)

    result = run_turn(db, row, "今天天气不错", ckpt=ckpt)

    assert result.intent != "weather"
    assert "weather__query" not in tool_names(result)
    assert http.calls == []
