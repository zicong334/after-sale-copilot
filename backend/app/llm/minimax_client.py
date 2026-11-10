"""Minimax 热备真实客户端(W10-1):与主用同款实现,只换三件套。

设计要点
--------
1. **同款实现,零漂移**:直接继承 :class:`RealLLMClient`,复用同一套 OpenAI 兼容
   请求体、错误分流(401/402/403/429/5xx)、重试与退避,以及**空正文护栏**
   (``content`` 为空 → :class:`LLMEmptyContentError`,绝不静默返回空串)。
   本模块**不另写一套**请求/空正文逻辑,避免两套实现随时间漂移。
2. **三件套参数化**:``api_key`` / ``base_url`` / ``model`` 均可由构造参数覆盖,
   否则依次读环境变量 ``LLM_FALLBACK_API_KEY`` / ``LLM_FALLBACK_BASE_URL`` /
   ``LLM_FALLBACK_MODEL``。**本模块不读 .env**(与 ``real_client`` 一致),
   保证 pytest 在有 .env 的机器上也绝不会误触真实 API。
3. **key 纪律**:缺 key 抛 :class:`LLMConfigError`(消息里绝不出现 key);
   :meth:`MinimaxRealClient.healthcheck` 的返回值与错误串同样不含 key
   (统一走 :meth:`MinimaxRealClient._scrub` 脱敏)。
4. **healthcheck 只打一次**:向 ``{base_url}/chat/completions`` 发一次极小
   ``max_tokens`` 的请求,返回可贴回执的**原文** dict。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.llm.real_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_RETRIES,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    LLMConfigError,
    RealLLMClient,
)

#: 备胎三件套的环境变量名(与主用的 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL 分离)。
ENV_API_KEY = "LLM_FALLBACK_API_KEY"
ENV_BASE_URL = "LLM_FALLBACK_BASE_URL"
ENV_MODEL = "LLM_FALLBACK_MODEL"

#: 备胎默认网关与默认模型(环境变量缺失时使用)。
DEFAULT_FALLBACK_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_FALLBACK_MODEL = "MiniMax-M3.1-Flash"

#: healthcheck 用的极小预算:只验连通性,不花钱。
_HEALTHCHECK_MAX_TOKENS = 1
#: healthcheck 错误片段保留长度。
_HEALTHCHECK_ERROR_LIMIT = 200


class MinimaxRealClient(RealLLMClient):
    """Minimax 热备客户端(W10-1 备胎)。

    与主用 :class:`RealLLMClient` 的差别只有两点:部署档位/供应商标识,以及
    三件套默认从 ``LLM_FALLBACK_*`` 环境变量取。其余行为(错误分流、空正文
    护栏、重试、usage 累计、stream_text)完全复用父类实现。
    """

    #: 部署档位(fallback=热备);由路由层(W10)读取。
    tier: str = "fallback"
    #: 供应商标识(写入 trace / 回执)。
    provider: str = "minimax"

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
        resolved_key = (api_key or os.environ.get(ENV_API_KEY) or "").strip()
        if not resolved_key:
            raise LLMConfigError(
                f"缺少 {ENV_API_KEY}:备胎真实模式需在进程环境中提供(本模块不读 .env)"
            )
        resolved_base_url = (
            base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_FALLBACK_BASE_URL
        ).rstrip("/")
        resolved_model = model or os.environ.get(ENV_MODEL) or DEFAULT_FALLBACK_MODEL
        # 三件套全部显式传下去:父类因此不会再去读 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL。
        super().__init__(
            api_key=resolved_key,
            base_url=resolved_base_url,
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
            capture_path=capture_path,
            price_in_per_mtok=price_in_per_mtok,
            price_out_per_mtok=price_out_per_mtok,
        )

    # ------------------------------------------------------------------ 探活
    def healthcheck(self) -> dict[str, Any]:
        """打**一次**真实连通性,返回可贴回执的原文 dict。

        返回字段固定为
        ``{"ok","provider","model","base_url","status_code","latency_ms","sample","error"}``;
        任何异常与错误串都经过 :meth:`_scrub` 脱敏,**绝不包含 key**。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0.0,
            "max_tokens": _HEALTHCHECK_MAX_TOKENS,
            "stream": False,
        }
        started = time.monotonic()
        status_code: int | None = None
        try:
            response = self._http.post(url, headers=headers, json=body)
            status_code = int(response.status_code)
            if status_code != 200:
                detail = self._scrub(str(response.text))[:_HEALTHCHECK_ERROR_LIMIT]
                return self._health_result(
                    ok=False,
                    status_code=status_code,
                    started=started,
                    error=f"HTTP {status_code}: {detail}",
                )
            try:
                data = response.json()
            except ValueError:
                return self._health_result(
                    ok=False,
                    status_code=status_code,
                    started=started,
                    error="响应不是合法 JSON",
                )
            return self._health_result(
                ok=True,
                status_code=status_code,
                started=started,
                sample=self._extract_sample(data),
                error=None,
            )
        except Exception as exc:  # 探活失败不得把异常抛给调用方
            message = self._scrub(f"{type(exc).__name__}: {exc}")
            return self._health_result(
                ok=False,
                status_code=status_code,
                started=started,
                error=message[:_HEALTHCHECK_ERROR_LIMIT],
            )

    # -------------------------------------------------------------- 内部实现
    def _health_result(
        self,
        *,
        ok: bool,
        status_code: int | None,
        started: float,
        sample: str = "",
        error: str | None,
    ) -> dict[str, Any]:
        """组装 healthcheck 返回值(字段固定,便于直接贴回执)。"""
        return {
            "ok": ok,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "status_code": status_code,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            "sample": sample,
            "error": error,
        }

    def _scrub(self, text: str) -> str:
        """把 key 从任意文本里抹掉(兜底:异常消息也可能带 URL/头)。"""
        key = getattr(self, "_api_key", "") or ""
        if key and key in text:
            return text.replace(key, "[已脱敏]")
        return text

    @staticmethod
    def _extract_sample(data: Any) -> str:
        """取正文首段做样例(只用于回执展示,不做业务判断)。"""
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = dict(choices[0].get("message") or {})
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            return ""
        return content[:120]


__all__ = [
    "DEFAULT_FALLBACK_BASE_URL",
    "DEFAULT_FALLBACK_MODEL",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "MinimaxRealClient",
]
