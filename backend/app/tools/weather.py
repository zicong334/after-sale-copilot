"""weather__query 工具:售后语境下的天气辅助信息(read)。

定位(W16 任务书):天气只在**售后履约语境**(暴雨/延误)下作为附注出现,
售后 Copilot 不做通用天气助手。数据源 Open-Meteo(公开、免 key):
- 地理编码:``https://geocoding-api.open-meteo.com/v1/search``
- 预报:``https://api.open-meteo.com/v1/forecast``

实现约束:
- 仅标准库 ``urllib``(不引第三方 HTTP 依赖);
- 单次请求超时 5s;
- 进程内 TTL 缓存 10 分钟(按城市键),同一会话不重复外呼;
- 上游失败/超时/城市不存在统一抛 ``ToolExecutionError("WEATHER_UPSTREAM_ERROR")``,
  由既有工具错误信封转成结构化失败回包,不让整张图炸掉;
- 城市名一律经 ``urlencode`` 拼进查询串(不拼进路径),杜绝参数注入;
- 天气现象由 WMO code 本地映射成中文,避免上游语言字段缺失导致英文串混入回复。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.tools.registry import ToolSpec, clean_schema, register
from app.tools.results import ToolContext, ToolExecutionError

logger = logging.getLogger("app.tools.weather")

TOOL_NAME = "weather__query"

#: 上游不可用类失败码(超时/HTTP 错误/返回体不可解析/城市不存在)。
CODE_UPSTREAM_ERROR = "WEATHER_UPSTREAM_ERROR"

#: 单次 HTTP 请求超时(秒)。
HTTP_TIMEOUT_S = 5.0

#: 进程内缓存 TTL(秒)——同一城市 10 分钟内不重复外呼。
CACHE_TTL_S = 600.0

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

#: 数据来源标识(回复里必须原样透出,配合固定免责口径)。
SOURCE_NAME = "Open-Meteo"

#: WMO 天气代码 → 中文现象(W16:天气现象直接进买家可见回复)。
WMO_WEATHER_TEXT: dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷阵雨伴冰雹",
}

#: 进程内 TTL 缓存:``{城市: (过期时刻, 数据)}``;只缓存成功结果。
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


class Input(BaseModel):
    """weather__query 入参:城市名(中文城市名)。

    允许少量标点是为了让 ``urlencode`` 的转义路径可测(单测会注入 ``&``/``#``):
    真正的防注入靠编码,不靠这里过滤。
    """

    model_config = ConfigDict(extra="forbid")

    city: str = Field(pattern=r"^[一-龥A-Za-z0-9 ,'\-&#?]{1,48}$")


INPUT_SCHEMA: dict[str, Any] = clean_schema(Input)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "temperature": {"type": ["number", "null"]},
        "apparent_temperature": {"type": ["number", "null"]},
        "weather": {"type": "string"},
        "weather_code": {"type": ["integer", "null"]},
        "temp_max": {"type": ["number", "null"]},
        "temp_min": {"type": ["number", "null"]},
        "precipitation_probability": {"type": ["number", "null"]},
        "observed_at": {"type": ["string", "null"]},
        "source": {"type": "string"},
    },
    "required": [
        "city",
        "temperature",
        "apparent_temperature",
        "weather",
        "weather_code",
        "temp_max",
        "temp_min",
        "precipitation_probability",
        "observed_at",
        "source",
    ],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name=TOOL_NAME,
    description=(
        "查询指定城市的当前天气与今日预报(实况温度、体感、天气现象、"
        "今日最高/最低温、降水概率)。仅用于售后履约语境的辅助说明(如暴雨可能"
        "影响派送);数据来自 Open-Meteo,仅供参考,不代表物流承诺。"
    ),
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    side_effect="read",
    timeout_s=HTTP_TIMEOUT_S,
    retry={"max_attempts": 1, "backoff_s": 0.0},
    idempotency_key=None,
    requires_confirm=False,
)


def _cache_get(city: str) -> dict[str, Any] | None:
    """取缓存;过期即清。"""
    entry = _CACHE.get(city)
    if entry is None:
        return None
    expires_at, payload = entry
    if expires_at <= time.monotonic():
        _CACHE.pop(city, None)
        return None
    return dict(payload)


def _cache_put(city: str, payload: dict[str, Any]) -> None:
    """写缓存(TTL 10 分钟)。"""
    _CACHE[city] = (time.monotonic() + CACHE_TTL_S, dict(payload))


def _fetch_json(url: str) -> dict[str, Any]:
    """取一次 JSON(超时 5s);任何上游异常统一转 WEATHER_UPSTREAM_ERROR。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": "after-sales-copilot/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("weather__query 上游请求失败: %s", exc.__class__.__name__)
        raise ToolExecutionError(
            CODE_UPSTREAM_ERROR,
            f"天气数据源请求失败({exc.__class__.__name__})",
            retryable=True,
            param="city",
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("weather__query 上游返回体不可解析")
        raise ToolExecutionError(
            CODE_UPSTREAM_ERROR,
            "天气数据源返回了无法解析的内容",
            retryable=True,
            param="city",
        ) from exc
    if not isinstance(payload, dict):
        raise ToolExecutionError(
            CODE_UPSTREAM_ERROR,
            "天气数据源返回了非预期的结构",
            retryable=True,
            param="city",
        )
    return payload


def _geocode(city: str) -> tuple[float, float, str]:
    """城市名 → (纬度, 经度, 展示名);查不到 → WEATHER_UPSTREAM_ERROR。"""
    query = urllib.parse.urlencode(
        {"name": city, "count": 1, "language": "zh", "format": "json"}
    )
    payload = _fetch_json(f"{GEOCODE_URL}?{query}")
    results = payload.get("results") or []
    if not results:
        raise ToolExecutionError(
            CODE_UPSTREAM_ERROR,
            f"未找到城市:{city}",
            retryable=False,
            param="city",
        )
    first = results[0]
    latitude = first.get("latitude")
    longitude = first.get("longitude")
    if latitude is None or longitude is None:
        raise ToolExecutionError(
            CODE_UPSTREAM_ERROR,
            "城市定位结果缺少经纬度",
            retryable=True,
            param="city",
        )
    name = str(first.get("name") or city)
    return float(latitude), float(longitude), name


def _daily_first(daily: dict[str, Any], key: str) -> Any:
    """取 daily 数组首值(缺失返回 None)。"""
    values = daily.get(key) or []
    if not isinstance(values, list) or not values:
        return None
    return values[0]


def _forecast(latitude: float, longitude: float) -> dict[str, Any]:
    """经纬度 → 天气实况 + 今日预报(缺字段留 None,不编造)。"""
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "current_weather": "true",
            "daily": (
                "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "timezone": "Asia/Shanghai",
        }
    )
    payload = _fetch_json(f"{FORECAST_URL}?{query}")
    current = payload.get("current_weather") or {}
    daily = payload.get("daily") or {}
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise ToolExecutionError(
            CODE_UPSTREAM_ERROR,
            "天气数据源返回了非预期的结构",
            retryable=True,
            param="city",
        )
    code = current.get("weathercode")
    phenomenon = WMO_WEATHER_TEXT.get(code) if isinstance(code, int) else None
    if not phenomenon:
        phenomenon = str(current.get("weather") or "未知")
    return {
        "temperature": current.get("temperature"),
        "apparent_temperature": current.get("apparent_temperature"),
        "weather": phenomenon,
        "weather_code": code if isinstance(code, int) else None,
        "temp_max": _daily_first(daily, "temperature_2m_max"),
        "temp_min": _daily_first(daily, "temperature_2m_min"),
        "precipitation_probability": _daily_first(
            daily, "precipitation_probability_max"
        ),
        "observed_at": current.get("time"),
    }


def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """查询天气;10 分钟内同城市命中缓存不再外呼。"""
    city = str(args["city"]).strip()
    if not city:
        raise ToolExecutionError(
            CODE_UPSTREAM_ERROR, "缺少城市名", retryable=False, param="city"
        )
    cached = _cache_get(city)
    if cached is not None:
        return cached

    latitude, longitude, resolved_name = _geocode(city)
    weather = _forecast(latitude, longitude)
    payload: dict[str, Any] = {
        "city": resolved_name or city,
        "temperature": weather["temperature"],
        "apparent_temperature": weather["apparent_temperature"],
        "weather": weather["weather"],
        "weather_code": weather["weather_code"],
        "temp_max": weather["temp_max"],
        "temp_min": weather["temp_min"],
        "precipitation_probability": weather["precipitation_probability"],
        "observed_at": weather["observed_at"],
        "source": SOURCE_NAME,
    }
    _cache_put(city, payload)
    return payload


register(SPEC, Input, handler)
