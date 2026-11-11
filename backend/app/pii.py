"""PII 脱敏管道(W7-1):出网前 mask,展示前 restore。

设计约束(与任务书 W7-1 对齐):
- **只替换,不改写**:每个命中片段换成一个稳定占位符(``[PHONE_1]`` /
  ``[ID_1]`` / ``[ADDR_1]``),同一轮内相同原文复用同一占位符,编号按该类型
  首次出现顺序递增。
- **映射表只在本进程内存**:``PIIMasker.mapping`` 是"占位符 → 原文",仅用于
  本轮响应的 ``pii_restore`` 反解;不落库、不写日志、不跨轮复用。
- **DB 存原文**:用户消息与 AI 回复落库前都要 restore;只有出网请求与
  ``tool_events.payload`` 用 mask 后文本。
- **幂等**:占位符本身不含手机号/身份证/地址特征,重复 mask 不会二次替换。

本模块不读环境变量、不发网络请求,纯内存字符串处理。
"""

from __future__ import annotations

from typing import Any, Iterator

from app.guardrails import PII_PATTERNS, PII_PLACEHOLDER_RE
from app.llm.base import LLMClient, Message

#: 占位符模板(类型前缀 + 本轮序号)。
PLACEHOLDER_TEMPLATE = "[{kind}_{index}]"

#: 未闭合 ``[`` 的最大等待长度(字符):超过该长度仍未闭合就认为它是正文里的
#: 普通方括号,立即放行。合法占位符(``[PHONE_1]``/``[ID_1]``/``[ADDR_1]``)
#: 内部只有十来个字符,16 足够覆盖(ISS-66)。
PLACEHOLDER_MAX_LEN = 16


class PIIMasker:
    """单轮 PII 映射器:mask 累积映射,restore 反解。"""

    def __init__(self) -> None:
        #: 占位符 → 原文(仅供本轮展示反解,不落库)
        self.mapping: dict[str, str] = {}
        #: 原文 → 占位符(同文复用同一占位符)
        self._reverse: dict[str, str] = {}
        #: 各类型已分配的最大序号
        self._counters: dict[str, int] = {}

    def mask(self, text: str) -> str:
        """把文本里的手机号/身份证/地址替换为占位符;无 PII 时原样返回。"""
        body = str(text or "")
        if not body:
            return body
        for kind, pattern in PII_PATTERNS:
            body = pattern.sub(lambda match, kind=kind: self._placeholder(kind, match.group(0)), body)
        return body

    def restore(self, text: str) -> str:
        """把本轮分配过的占位符换回原文;未知占位符原样保留(不猜)。"""
        body = str(text or "")
        if not body or not self.mapping:
            return body
        return PII_PLACEHOLDER_RE.sub(lambda match: self.mapping.get(match.group(0), match.group(0)), body)

    def has_pii(self) -> bool:
        """本轮是否至少命中过一次 PII。"""
        return bool(self.mapping)

    def restore_mapping(self) -> dict[str, str]:
        """给 chat 响应 ``pii_restore`` 的映射副本(避免调用方改内部状态)。"""
        return dict(self.mapping)

    def _placeholder(self, kind: str, value: str) -> str:
        existing = self._reverse.get(value)
        if existing is not None:
            return existing
        index = self._counters.get(kind, 0) + 1
        self._counters[kind] = index
        placeholder = PLACEHOLDER_TEMPLATE.format(kind=kind, index=index)
        self.mapping[placeholder] = value
        self._reverse[value] = placeholder
        return placeholder


def mask_messages(messages: list[Message], masker: PIIMasker) -> list[Message]:
    """copy 一份消息列表并逐条 mask content(role 原样保留)。"""
    masked: list[Message] = []
    for message in messages or ():
        masked.append(
            {
                "role": str(message.get("role") or "user"),
                "content": masker.mask(str(message.get("content") or "")),
            }
        )
    return masked


class MaskingLLM(LLMClient):
    """``LLMClient`` 装饰器:出网前 mask 全部 message,回包 restore 占位符。

    装饰器只改 ``complete`` 的输入/输出文本,其余属性(model_name / usage /
    价格等)全部透明代理给真实客户端,编排层无感知。
    """

    def __init__(self, inner: LLMClient, masker: PIIMasker | None = None) -> None:
        self.inner = inner
        self.masker = masker if masker is not None else PIIMasker()

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    def complete(self, messages: list[Message], schema: dict[str, Any] | None = None) -> str:
        masked = mask_messages(messages, self.masker)
        return self.masker.restore(self.inner.complete(masked, schema))

    def stream_text(
        self,
        messages: list[Message],
        schema: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """真流式(W10):出网前 mask,回包逐段 restore。

        占位符可能被切在两块之间(如 ``[PHONE`` + ``_1]``),这里按"完整占位符"
        边界切分:未闭合的 ``[`` 片段留到下一块再还原,避免吐出半个占位符。
        """
        masked = mask_messages(messages, self.masker)
        pending = ""
        for piece in self.inner.stream_text(masked, schema):
            pending += piece
            cut = self._safe_cut(pending)
            if cut <= 0:
                continue
            text, pending = pending[:cut], pending[cut:]
            if text:
                yield self.masker.restore(text)
        if pending:
            yield self.masker.restore(pending)

    @staticmethod
    def _safe_cut(text: str) -> int:
        """返回可安全吐出的前缀长度(不切断占位符)。

        只有"看起来像占位符"的未闭合 ``[`` 才压住等待下一块:若 ``[`` 之后已
        积累超过 ``PLACEHOLDER_MAX_LEN`` 个字符仍未闭合,按普通正文立即放行
        —— 否则正文里一个孤立的 ``[`` 会把后续所有 delta 卡到整段生成结束
        (ISS-66)。
        """
        start = text.rfind("[")
        if start == -1:
            return len(text)
        end = text.rfind("]")
        if end == -1 or end < start:
            # 未闭合:很近(可能是被切断的占位符)就等下一块,太远则当正文放行。
            if len(text) - start - 1 > PLACEHOLDER_MAX_LEN:
                return len(text)
            return start
        return end + 1

    def __getattr__(self, name: str) -> Any:
        # 未定义的属性(usage / last_usage / 价格表等)一律代理给内层客户端。
        return getattr(self.inner, name)
