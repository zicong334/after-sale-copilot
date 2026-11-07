"""记忆注入文案:任务开始时注入一次,固定顺序、声明可能过时、明示不可执行边界。

格式(任务书 W3-B,固定文案):

    以下是买家历史偏好,可能过时,仅用于尺码推荐,不得作为价格/退款/承诺依据:
    - [preference/size_recommendation] 尺码偏好:M 偏紧,优先推荐 L

- 空记忆:显式输出 ``无历史偏好``(不猜、不编);
- 只注入 ``status='active' + executable=True + 未过期 + decay 达标`` 的记忆,
  定价指令(status=quarantined)永远不会出现在这里;
- 排序固定 (scope, type, created_at, id),同一份记忆每次注入文本一致(利于 KV cache)。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.memory.models import Memory
from app.memory.store import query_memories
from app.memory.whitelist import scope_label

#: 注入前缀模板;``{scope}`` 为本次注入记忆的适用范围标签
INJECTION_PREFIX = (
    "以下是买家历史偏好,可能过时,仅用于{scope},不得作为价格/退款/承诺依据:"
)

#: 空记忆的显式声明
EMPTY_INJECTION = "无历史偏好"


def build_injection(entries: list[Memory]) -> dict[str, Any]:
    """把查询到的记忆渲染成固定格式的注入包(纯函数,便于单测)。"""
    if not entries:
        return {"text": EMPTY_INJECTION, "count": 0, "scopes": []}

    scopes = sorted({str(entry.scope) for entry in entries})
    label = scope_label(scopes[0]) if len(scopes) == 1 else "售后咨询"
    lines = [INJECTION_PREFIX.format(scope=label)]
    for entry in entries:
        lines.append(f"- [{entry.type}/{entry.scope}] {entry.text}")
    return {"text": "\n".join(lines), "count": len(entries), "scopes": scopes}


def load_injection(
    session: Session,
    *,
    merchant_id: str,
    user_id: str,
    scopes: list[str] | tuple[str, ...] | None = None,
    now=None,
) -> dict[str, Any]:
    """任务开始时读取一次买家记忆并生成注入文案。"""
    entries = query_memories(
        session,
        merchant_id=merchant_id,
        user_id=user_id,
        scopes=scopes,
        now=now,
    )
    return build_injection(entries)
