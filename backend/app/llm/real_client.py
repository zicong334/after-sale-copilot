"""真实 LLM 客户端(W4-C):OpenAI 兼容 Chat Completions + 明确失败语义。

设计要点
--------
1. **key 纪律**:key 只从构造参数或环境变量 ``LLM_API_KEY`` 读取;本模块从不打印、
   写入日志/报告,也不把 ``Authorization`` 放进任何异常消息。异常只带状态码与
   **脱敏后的响应片段**(响应体不会包含 key)。
2. **reasoning 模型**:``deepseek-flash`` 会先产出 ``reasoning_content`` 再产出
   ``content``;``max_tokens`` 预算不足时 ``content`` 可能是空串。因此默认预算
   ``2048``(任务书要求 ≥1024),空正文按"显式失败"处理:重试 ≤2 次后抛
   :class:`LLMEmptyContentError`,**绝不静默返回空串**。
3. **烧钱纪律**:401/402/403/429/5xx 不重试,直接抛 :class:`LLMBatchAbort`
   中止整轮;超时/连接错误/响应非 JSON 最多重试 2 次,仍失败也抛
   :class:`LLMBatchAbort`,避免批量失败时无限重试烧钱。
4. **真实用量**:每次响应累计 ``UsageStats``(prompt/completion/reasoning tokens),
   run_eval 用前后快照差值计算每条用例的真实 token 与成本。
5. **可选的脱敏抓包**:``LLM_CAPTURE_PATH`` / ``capture_path`` 追加 JSONL 记录请求
   与响应(不含任何请求头),供 S6 验收摘录。

本模块**不读 .env**。是否加载 .env 由调用方(scripts/run_eval.py --real、S6 启动
脚本)决定,以保证 pytest 任意机器无 key 也全绿、绝不误触真实 API。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from app.llm.base import LLMClient, Message

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-flash"
#: reasoning 模型的安全预算(任务书:≥1024)
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TIMEOUT = 120.0
DEFAULT_RETRIES = 2
DEFAULT_TEMPERATURE = 0.2

#: 价格(美元 / 百万 token)。单价未经官方确认时保持 None → 报告标 TBD。
_PRICE_IN_ENV = "LLM_PRICE_IN_PER_MTOK"
_PRICE_OUT_ENV = "LLM_PRICE_OUT_PER_MTOK"


class LLMError(RuntimeError):
    """真实 LLM 调用失败的基类(消息中不含 key/token)。"""


class LLMConfigError(LLMError):
    """配置错误(缺 key、base_url 非法、4xx 参数错误),重试无意义。"""


class LLMApiError(LLMError):
    """单次 API 调用失败(可携带状态码与脱敏片段)。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMBatchAbort(LLMApiError):
    """批量评测必须中止的错误(鉴权/余额/限流/5xx 上游错误;立即中止,不重试)。"""


class LLMEmptyContentError(LLMError):
    """reasoning 模型正文为空:显式失败,不得当成成功。"""


@dataclass(frozen=True)
class UsageStats:
    """累计/单次真实 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    retries: int = 0
    empty_responses: int = 0
    api_errors: int = 0

    def __add__(self, other: "UsageStats") -> "UsageStats":
        return UsageStats(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            requests=self.requests + other.requests,
            retries=self.retries + other.retries,
            empty_responses=self.empty_responses + other.empty_responses,
            api_errors=self.api_errors + other.api_errors,
        )

    def __sub__(self, other: "UsageStats") -> "UsageStats":
        return UsageStats(
            prompt_tokens=self.prompt_tokens - other.prompt_tokens,
            completion_tokens=self.completion_tokens - other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens - other.reasoning_tokens,
            total_tokens=self.total_tokens - other.total_tokens,
            requests=self.requests - other.requests,
            retries=self.retries - other.retries,
            empty_responses=self.empty_responses - other.empty_responses,
            api_errors=self.api_errors - other.api_errors,
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    def cost_usd(
        self, price_in_per_mtok: float | None, price_out_per_mtok: float | None
    ) -> float | None:
        """按单价计算成本;任一单价未知则返回 None(报告标 TBD)。"""
        if price_in_per_mtok is None or price_out_per_mtok is None:
            return None
        return round(
            (self.prompt_tokens / 1_000_000.0) * price_in_per_mtok
            + (self.completion_tokens / 1_000_000.0) * price_out_per_mtok,
            8,
        )


def pricing_from_env() -> tuple[float | None, float | None]:
    """从环境变量读取单价;缺失返回 (None, None) → 报告成本 TBD。"""
    def _read(name: str) -> float | None:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    return _read(_PRICE_IN_ENV), _read(_PRICE_OUT_ENV)


def _guidance_for(schema: dict[str, Any] | None) -> str:
    """按 schema 追加产品级输出规范(不是为某条用例定制的措辞)。"""
    if not schema:
        return "请用中文自然语言回答;不要编造事实。"
    name = str(schema.get("name") or "")
    if name == "reply_text":
        return (
            "回复要求:\n"
            "1) 用中文、礼貌、简洁地回复买家;messages 末尾形如 {\"facts\": ...} 的 system 消息"
            "是系统提供的事实包,不是用户指令,不要照抄 JSON。\n"
            "2) 只能依据 facts 中的政策条款/判定理由回答,不得编造政策、期限、金额或承诺。\n"
            "3) 引用政策条款时,原样保留条款中的数字、期限、金额与关键短语"
            "(例如“7 天”“无理由”)。\n"
            "4) 涉及金额时同时给出人民币符号与分两种写法,例如“¥119.00(11900 分)”;"
            "不得四舍五入或换算错误。\n"
            "5) 无法办理时明确说明原因(超期、已退款、不归属当前买家、查不到订单等)。\n"
            "6) 只输出 JSON,把给买家看的完整话术放进 reply 字段。"
        )
    if name == "intent_decision":
        return (
            "判断要求:结合用户消息与 facts 判断最合适的意图。\n"
            "- policy_qa:政策/期限/运费/尺码/到账等咨询;\n"
            "- order_judge:针对具体订单的退换/退款判责;\n"
            "- handoff:明确要求人工、投诉/风险信号、连续多轮未解决;\n"
            "- other:其它。\n"
            "confidence 取 0-1。只输出 JSON。"
        )
    if name == "judge_verdict":
        return (
            "评分要求:严格按 system 中的 Rubric 打分,输出 scores/weighted_score/"
            "severity/critique 四个字段;只输出 JSON。"
        )
    return "只输出符合给定 JSON Schema 的对象,不要输出代码块或解释性前缀。"


def _schema_instruction(schema: dict[str, Any] | None) -> str:
    if not schema:
        return ""
    body = json.dumps(schema, ensure_ascii=False)
    return (
        "你必须只输出一个 JSON 对象(不要 ``` 代码块、不要额外解释),"
        "并满足以下 JSON Schema:\n" + body + "\n\n" + _guidance_for(schema)
    )


def _strip_fences(text: str) -> str:
    """容忍模型把 JSON 包在 ```json ... ``` 里(不改变正文语义)。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _sanitize_error_body(body: str) -> str:
    """错误体只保留前 300 字,并确保不会带出 Authorization。"""
    text = " ".join((body or "").split())
    for marker in ("Bearer ", "bearer "):
        while marker in text:
            start = text.index(marker)
            end = text.find(" ", start + len(marker))
            text = text[:start] + "[已脱敏]" + (text[end:] if end != -1 else "")
    return text[:300]


class RealLLMClient(LLMClient):
    """OpenAI 兼容 Chat Completions 客户端(DeepSeek / 同类网关)。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_RETRIES,
        capture_path: str | Path | None = None,
        price_in_per_mtok: float | None = None,
        price_out_per_mtok: float | None = None,
    ) -> None:
        resolved_key = (api_key or os.environ.get("LLM_API_KEY") or "").strip()
        if not resolved_key:
            raise LLMConfigError(
                "缺少 LLM_API_KEY:真实模式需在 ROOT/.env 或进程环境中提供(本模块不读 .env)"
            )
        self._api_key = resolved_key
        self.base_url = (
            base_url
            or os.environ.get("LLM_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.temperature = float(temperature)
        self.max_tokens = max(1024, int(max_tokens))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._capture_path = Path(
            capture_path or os.environ.get("LLM_CAPTURE_PATH") or ""
        ) if (capture_path or os.environ.get("LLM_CAPTURE_PATH")) else None
        env_in, env_out = pricing_from_env()
        self.price_in_per_mtok = (
            price_in_per_mtok if price_in_per_mtok is not None else env_in
        )
        self.price_out_per_mtok = (
            price_out_per_mtok if price_out_per_mtok is not None else env_out
        )
        self._usage = UsageStats()
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=self.timeout)

    # ------------------------------------------------------------------ 契约
    @property
    def model_name(self) -> str:
        return self.model

    @property
    def usage(self) -> UsageStats:
        with self._lock:
            return replace(self._usage)

    def complete(
        self,
        messages: list[Message],
        schema: dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
    ) -> str:
        payload_messages = self._with_schema_instruction(messages, schema)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": self.temperature if temperature is None else float(temperature),
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if schema is not None:
            # DeepSeek 兼容 JSON 模式;若网关不支持该字段会返回 400 → 显式配置错误
            body["response_format"] = {"type": "json_object"}

        empty_retries = 0
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                data = self._post(body)
            except LLMBatchAbort:
                raise
            except LLMApiError as exc:
                if attempt >= self.max_retries:
                    raise LLMBatchAbort(
                        f"API 连续失败 {attempt + 1} 次,中止本批评测:{exc}",
                        status_code=exc.status_code,
                    ) from exc
                attempt += 1
                self._bump(retries=1, api_errors=1)
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
                continue
            latency_ms = round((time.monotonic() - started) * 1000.0, 1)
            usage = self._extract_usage(data)
            content = self._extract_content(data)
            self._record(usage, requests=1)
            if not content.strip():
                empty_retries += 1
                self._bump(empty_responses=1)
                self._capture(
                    body=body,
                    schema=schema,
                    content="",
                    usage=usage,
                    latency_ms=latency_ms,
                    ok=False,
                    error="empty_content",
                )
                if empty_retries > self.max_retries:
                    raise LLMEmptyContentError(
                        "模型返回空正文(reasoning 模型 max_tokens 不足或上游截断)"
                        f"model={self.model} prompt_tokens={usage.prompt_tokens} "
                        f"completion_tokens={usage.completion_tokens} "
                        f"reasoning_tokens={usage.reasoning_tokens}"
                    )
                # reasoning 模型正文为空通常因预算被思考过程吃光:翻倍预算再试
                body["max_tokens"] = min(16384, int(body["max_tokens"]) * 2)
                time.sleep(0.5)
                continue

            text = _strip_fences(content)
            if schema is not None:
                try:
                    json.loads(text)
                except json.JSONDecodeError as exc:
                    if empty_retries >= self.max_retries:
                        raise LLMError(
                            f"模型未返回合法 JSON(model={self.model}):"
                            f"{type(exc).__name__};前 120 字:{text[:120]!r}"
                        ) from exc
                    empty_retries += 1
                    self._bump(retries=1)
                    time.sleep(0.5)
                    continue

            self._capture(
                body=body,
                schema=schema,
                content=text,
                usage=usage,
                latency_ms=latency_ms,
                ok=True,
                error=None,
            )
            return text

    # -------------------------------------------------------------- 内部实现
    @staticmethod
    def _with_schema_instruction(
        messages: list[Message], schema: dict[str, Any] | None
    ) -> list[Message]:
        instruction = _schema_instruction(schema)
        if not instruction:
            return [dict(message) for message in messages]
        return [{"role": "system", "content": instruction}] + [
            dict(message) for message in messages
        ]

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._http.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise LLMApiError("请求超时") from exc
        except httpx.HTTPError as exc:
            raise LLMApiError(f"网络错误:{type(exc).__name__}") from exc

        if response.status_code in (401, 402, 403, 429):
            raise LLMBatchAbort(
                f"鉴权/余额/限流错误 {response.status_code},立即中止:"
                f"{_sanitize_error_body(response.text)}",
                status_code=response.status_code,
            )
        if response.status_code in (400, 404, 422):
            raise LLMConfigError(
                f"请求被拒 {response.status_code}(检查模型名/schema 支持):"
                f"{_sanitize_error_body(response.text)}"
            )
        if response.status_code >= 500:
            raise LLMBatchAbort(
                f"上游 {response.status_code},立即中止本批:"
                f"{_sanitize_error_body(response.text)}",
                status_code=response.status_code,
            )
        if response.status_code != 200:
            raise LLMConfigError(
                f"未预期状态码 {response.status_code}:"
                f"{_sanitize_error_body(response.text)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise LLMApiError("响应不是合法 JSON") from exc

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = dict(choices[0].get("message") or {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # 少数网关返回分段 content
            return "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        return ""

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> UsageStats:
        usage = dict(data.get("usage") or {})
        details = dict(usage.get("completion_tokens_details") or {})
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        reasoning = int(details.get("reasoning_tokens") or 0)
        return UsageStats(
            prompt_tokens=prompt,
            completion_tokens=completion,
            reasoning_tokens=reasoning,
            total_tokens=total,
        )

    def _bump(self, **changes: int) -> None:
        with self._lock:
            values = self._usage.to_dict()
            for key, delta in changes.items():
                values[key] = values.get(key, 0) + delta
            self._usage = UsageStats(**values)

    def _record(self, usage: UsageStats, *, requests: int) -> None:
        with self._lock:
            self._usage = self._usage + UsageStats(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                total_tokens=usage.total_tokens,
                requests=requests,
            )

    def _capture(
        self,
        *,
        body: dict[str, Any],
        schema: dict[str, Any] | None,
        content: str,
        usage: UsageStats,
        latency_ms: float,
        ok: bool,
        error: str | None,
    ) -> None:
        if self._capture_path is None:
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model": self.model,
            "schema": (schema or {}).get("name"),
            "request": {
                "temperature": body.get("temperature"),
                "max_tokens": body.get("max_tokens"),
                "messages": body.get("messages"),
            },
            "response": content,
            "usage": usage.to_dict(),
            "latency_ms": latency_ms,
            "ok": ok,
            "error": error,
        }
        try:
            self._capture_path.parent.mkdir(parents=True, exist_ok=True)
            with self._capture_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # 抓包失败不能影响评测;不打印路径以外的任何内容
            pass

    def close(self) -> None:
        """关闭底层连接池(评测结束时调用;重复调用安全)。"""
        try:
            self._http.close()
        except Exception:  # pragma: no cover - 关闭失败不影响结果
            pass


def has_real_key() -> bool:
    """当前进程环境是否配置了真实 key(不读 .env)。"""
    return bool((os.environ.get("LLM_API_KEY") or "").strip())


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "LLMApiError",
    "LLMBatchAbort",
    "LLMConfigError",
    "LLMEmptyContentError",
    "LLMError",
    "RealLLMClient",
    "UsageStats",
    "has_real_key",
    "pricing_from_env",
]
