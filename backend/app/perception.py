"""perception.py — 感知层:把外部进线归一化为 ``Percept``。

范围(W2-C):
1. 文本清洗:去零宽/不可见控制字符、去首尾空白;空文本直接拒绝;
2. 超长截断:超过 4000 字截断,并显式标记 ``truncated`` / ``original_length``(不静默丢数据);
3. 附件只接受 http(s) URL:拒收 base64 / data URI(避免把图片二进制塞进链路),
   只保留 URL 交给后续步骤(图片理解留给 W4 之外的专门环节)。

边界:本模块是**纯函数,无 IO**——不落库、不写 trace、不调工具/LLM。
这正是 PRD §5 六节点图的第一节点 ``perceive``:可单测、可复用、可被 W3 的图直接调用。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: 单条消息文本上限(字符)。超出截断并标记,不静默丢弃。
MAX_TEXT_LENGTH = 4000

#: 零宽 / 不可见字符(客户从网页复制粘贴时常见,会污染检索与日志)
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"

#: 允许的附件协议:只收 URL,不收二进制
ALLOWED_ATTACHMENT_SCHEMES = ("http://", "https://")

#: locale / channel 的兜底值
DEFAULT_LOCALE = "zh-CN"
DEFAULT_CHANNEL = "web"


class PerceptionError(ValueError):
    """感知层校验失败(路由层据此返回 400)。"""

    def __init__(self, message: str, *, field: str = "text") -> None:
        super().__init__(message)
        self.field = field


class Percept(BaseModel):
    """归一化后的进线内容(不可变)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    attachments: list[str] = Field(default_factory=list, description="附件 URL 列表(仅 http/https)")
    locale: str = DEFAULT_LOCALE
    channel: str = DEFAULT_CHANNEL
    truncated: bool = False
    original_length: int | None = None


def normalize(
    text: Any,
    *,
    attachments: Any = None,
    locale: Any = None,
    channel: Any = None,
) -> Percept:
    """把原始进线归一化为 Percept;非法输入抛 ``PerceptionError``。"""
    if not isinstance(text, str):
        raise PerceptionError("消息体 text 必须是字符串", field="text")

    original_length = len(text)
    cleaned = text.translate({ord(ch): None for ch in ZERO_WIDTH_CHARS}).strip()
    if not cleaned:
        raise PerceptionError("消息内容为空(清洗后无有效文本)", field="text")

    truncated = len(cleaned) > MAX_TEXT_LENGTH
    if truncated:
        cleaned = cleaned[:MAX_TEXT_LENGTH]

    return Percept(
        text=cleaned,
        attachments=normalize_attachments(attachments),
        locale=_normalize_tag(locale, DEFAULT_LOCALE, field="locale"),
        channel=_normalize_tag(channel, DEFAULT_CHANNEL, field="channel"),
        truncated=truncated,
        original_length=original_length if truncated else None,
    )


def normalize_attachments(raw: Any) -> list[str]:
    """校验附件:必须是 http(s) URL 列表;拒收 base64 / data URI。"""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise PerceptionError("attachments 必须是 URL 列表", field="attachments")

    urls: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise PerceptionError("attachments 每一项都必须是字符串 URL", field="attachments")
        value = item.strip()
        lowered = value.lower()
        if lowered.startswith("data:") or ";base64," in lowered:
            raise PerceptionError(
                "附件不接受 base64/data URI,请先上传为可访问的 http(s) URL",
                field="attachments",
            )
        if not lowered.startswith(ALLOWED_ATTACHMENT_SCHEMES):
            raise PerceptionError(
                "附件只接受 http(s) URL(不支持本地路径或其它协议)",
                field="attachments",
            )
        if value not in urls:  # 去重但保持顺序
            urls.append(value)
    return urls


def excerpt(percept: Percept, limit: int = 60) -> str:
    """取一段短文本用于会话列表/日志(不用于回复)。"""
    text = percept.text
    return text if len(text) <= limit else text[:limit] + "…"


def _normalize_tag(value: Any, default: str, *, field: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise PerceptionError(f"{field} 必须是字符串", field=field)
    tag = value.strip()
    if not tag:
        return default
    if len(tag) > 32:
        raise PerceptionError(f"{field} 过长(最多 32 字符)", field=field)
    return tag
