"""memory 写入 / 软删除 / 查询 / 任务结束抽取。

写入时机(任务书 W3-B):**只有任务结束(图终态)才抽取偏好并写记忆**,对话循环
与"待确认"挂起轮都不写(nodes.compose 里用调用计数桩证明)。

MOCK 说明(禁止假实现,见 00_通用约定 §2.3):
- ``extract_and_write`` 先按真实模型的契约调用 ``llm.complete(SCHEMA_MEMORY_EXTRACT)``
  并解析 JSON,这条路径供 W4-C 换真实模型时直接使用;
- 当前注入的是 ``FakeLLM``,它只认 intent/reply 两个 schema,对抽取 schema 返回
  "未支持的 schema"。因此解析失败时回退到**确定性规则抽取**(与 FakeLLM 同源的
  MOCK 实现),代码与回执均显式标注 ``MOCK``。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import utc_now
from app.llm import LLMClient, build_facts_message
from app.memory.models import Memory
from app.memory.whitelist import (
    DEFAULT_SCOPE,
    MIN_DECAY_SCORE,
    can_write,
    default_ttl_days,
    is_pricing_instruction,
)

#: 抽取 schema 契约(真实模型按此输出;FakeLLM 不支持 → 回退规则抽取,MOCK)
SCHEMA_MEMORY_EXTRACT: dict[str, Any] = {
    "name": "memory_extract",
    "description": (
        "从买家话语抽取可长期保存的偏好,返回 JSON:"
        '{"memories":[{"type":"preference|fact|episode|procedure",'
        '"text":"...","scope":"size_recommendation|pricing|after_sale|logistics",'
        '"source":"user_stated|order_derived|agent_inferred","importance":0.0-1.0}]}'
    ),
}

#: 抽取提示词(真实模型路径用;FakeLLM 忽略它)
_EXTRACT_SYSTEM = (
    "你是售后客服的长期记忆抽取器。只抽取买家明确表达的长期偏好,"
    "不要抽取订单事实或店铺政策;不要改价/承诺;只输出 JSON。"
)


# ---------------------------------------------------------------------------
# 写入 / 软删除
# ---------------------------------------------------------------------------


def create_memory(
    session: Session,
    *,
    merchant_id: str,
    user_id: str,
    memory_type: str,
    text: str,
    source: str,
    scope: str | None = None,
    session_id: str | None = None,
    importance: float = 0.5,
    ttl_days: int | None = None,
    now=None,
    executable: bool | None = None,
) -> Memory:
    """写一条记忆(白名单校验 + 定价指令自动隔离 + 默认 TTL + 幂等去重)。

    - 白名单拒绝:类型/来源非法或类型×来源不允许 → ``ValueError``;
    - 定价/折扣/承诺:强制 ``executable=False + status='quarantined'``;
    - ``session_id=None`` 表示长期记忆(跨会话);
    - 同一 (merchant, user) 下相同 type+text 的未删除记忆不重复写(节点重跑幂等)。
    """
    if not can_write(memory_type, source):
        raise ValueError(f"记忆白名单拒绝:type={memory_type!r} source={source!r}")
    if not str(text or "").strip():
        raise ValueError("记忆文本不能为空")

    existing = session.scalars(
        select(Memory).where(
            Memory.merchant_id == merchant_id,
            Memory.user_id == user_id,
            Memory.type == memory_type,
            Memory.text == text,
            Memory.deleted_at.is_(None),
            Memory.status.in_(("active", "quarantined")),
        )
    ).first()
    if existing is not None:
        return existing

    now = now or utc_now()
    quarantined = is_pricing_instruction(text)
    if executable is False:
        quarantined = True
    if ttl_days is None:
        ttl_days = default_ttl_days(memory_type)
    expires_at = now + timedelta(days=ttl_days) if ttl_days else None

    row = Memory(
        merchant_id=merchant_id,
        user_id=user_id,
        session_id=session_id,
        type=memory_type,
        importance=float(importance),
        text=text,
        source=source,
        executable=not quarantined and executable is not False,
        status="quarantined" if quarantined else "active",
        scope=scope or DEFAULT_SCOPE,
        ttl_expires_at=expires_at,
        decay_score=1.0,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def soft_delete(session: Session, memory_id: str, *, now=None) -> bool:
    """软删除一条记忆(只置 ``deleted_at``,不物理删除);已删/不存在返回 False。"""
    row = session.get(Memory, memory_id)
    if row is None or row.deleted_at is not None:
        return False
    row.deleted_at = now or utc_now()
    session.flush()
    return True


# ---------------------------------------------------------------------------
# 查询(注入与衰减共用)
# ---------------------------------------------------------------------------


def query_memories(
    session: Session,
    *,
    merchant_id: str,
    user_id: str,
    scopes: list[str] | tuple[str, ...] | None = None,
    now=None,
    min_decay: float = MIN_DECAY_SCORE,
) -> list[Memory]:
    """查询"可注入"的记忆:active + executable + 未过期 + 未软删 + decay 达标。

    固定排序 (scope, type, created_at, id):同一份记忆每次注入的顺序完全一致,
    利于 KV cache(03§4.2.4 三时机之一)。
    """
    now = now or utc_now()
    stmt = select(Memory).where(
        Memory.merchant_id == merchant_id,
        Memory.user_id == user_id,
        Memory.deleted_at.is_(None),
        Memory.status == "active",
        Memory.executable.is_(True),
        Memory.decay_score >= min_decay,
        or_(Memory.ttl_expires_at.is_(None), Memory.ttl_expires_at > now),
    )
    if scopes:
        stmt = stmt.where(Memory.scope.in_(list(scopes)))
    stmt = stmt.order_by(
        Memory.scope.asc(),
        Memory.type.asc(),
        Memory.created_at.asc(),
        Memory.id.asc(),
    )
    return list(session.scalars(stmt))


# ---------------------------------------------------------------------------
# 任务结束抽取(MOCK:规则回退;真实模型走 SCHEMA_MEMORY_EXTRACT)
# ---------------------------------------------------------------------------


#: 尺码偏紧/偏松表达,如 "M 偏紧"、"L码有点大"
_SIZE_RE = re.compile(
    r"(?P<size>XXXL|XXL|XL|L|M|S)\s*(?:码|号)?\s*"
    r"(?:偏|太|有点|略微|比较)\s*(?P<fit>紧|小|松|大|宽)"
)

#: 未来尺码偏好表达,如 "以后推荐 L"、"之后都选 XL"
_PREFER_RE = re.compile(
    r"(?:以后|之后|下次|往后|未来)\s*(?:都|就|优先|直接|一直)?\s*"
    r"(?:推荐|推|选|买|要|发)\s*(?P<size>XXXL|XXL|XL|L|M|S)"
)

_NEXT_LARGER = {"S": "M", "M": "L", "L": "XL", "XL": "XXL", "XXL": "XXXL", "XXXL": "XXXL"}
_NEXT_SMALLER = {"XXXL": "XXL", "XXL": "XL", "XL": "L", "L": "M", "M": "S", "S": "S"}

#: 分句符(定价指令按句隔离,避免把整段话写进备注)
_CLAUSE_SPLIT_RE = re.compile(r"[;；。!?！?\n]+")
_CLAUSE_LEAD_RE = re.compile(r"^(?:另外|还有|并且|同时|以及)\s*")


def _llm_candidates(text: str, llm: LLMClient) -> list[dict[str, Any]] | None:
    """真实模型抽取路径;FakeLLM 不支持该 schema → 返回 None 触发规则回退(MOCK)。"""
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": text},
        build_facts_message({"task": "memory_extract", "text": text}),
    ]
    try:
        raw = llm.complete(messages, SCHEMA_MEMORY_EXTRACT)
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None
    except Exception:  # noqa: BLE001 - 抽取失败必须回退规则,不能影响本轮回复
        return None
    memories = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(memories, list):
        return None
    candidates: list[dict[str, Any]] = []
    for item in memories:
        if not isinstance(item, dict):
            continue
        memory_type = str(item.get("type") or "preference")
        source = str(item.get("source") or "user_stated")
        candidate_text = str(item.get("text") or "").strip()
        if not candidate_text:
            continue
        candidates.append(
            {
                "type": memory_type,
                "source": source,
                "text": candidate_text,
                "scope": str(item.get("scope") or DEFAULT_SCOPE),
                "importance": float(item.get("importance") or 0.5),
            }
        )
    return candidates or None


def _size_candidate(text: str) -> dict[str, Any] | None:
    """规则抽取尺码偏好(MOCK:与 FakeLLM 同源,替代真实模型)。"""
    match = _SIZE_RE.search(text)
    prefer = _PREFER_RE.search(text)
    if match is None and prefer is None:
        return None
    if match is not None:
        origin = match.group("size")
        fit = match.group("fit")
        if prefer is not None:
            recommend = prefer.group("size")
        elif fit in ("紧", "小"):
            recommend = _NEXT_LARGER[origin]
        else:
            recommend = _NEXT_SMALLER[origin]
        memory_text = f"尺码偏好:{origin} 偏{fit},优先推荐 {recommend}"
    else:
        recommend = prefer.group("size")
        memory_text = f"尺码偏好:优先推荐 {recommend}"
    return {
        "type": "preference",
        "source": "user_stated",
        "text": memory_text,
        "scope": "size_recommendation",
        "importance": 0.6,
    }


def _pricing_candidate(text: str) -> dict[str, Any] | None:
    """规则抽取定价指令并要求隔离(MOCK);返回 executable=False 的备注候选。"""
    if not is_pricing_instruction(text):
        return None
    clause = ""
    for raw in _CLAUSE_SPLIT_RE.split(text):
        current = _CLAUSE_LEAD_RE.sub("", raw.strip())
        if current and is_pricing_instruction(current):
            clause = current
            break
    if not clause:
        clause = text.strip()
    return {
        "type": "preference",
        "source": "user_stated",
        "text": f"定价指令(已隔离):{clause}",
        "scope": "pricing",
        "importance": 0.4,
        "executable": False,
    }


def extract_candidates(text: str, llm: LLMClient | None) -> list[dict[str, Any]]:
    """抽取候选记忆:真实模型优先,失败回退规则(MOCK)。"""
    if not str(text or "").strip():
        return []
    if llm is not None:
        candidates = _llm_candidates(text, llm)
        if candidates is not None:
            return candidates
    # MOCK: FakeLLM 只认 intent/reply schema,这里用确定性规则补齐
    candidates = []
    size_candidate = _size_candidate(text)
    if size_candidate is not None:
        candidates.append(size_candidate)
    pricing_candidate = _pricing_candidate(text)
    if pricing_candidate is not None:
        candidates.append(pricing_candidate)
    return candidates


def extract_and_write(
    session: Session,
    *,
    merchant_id: str,
    user_id: str,
    text: str,
    session_id: str | None = None,
    llm: LLMClient | None = None,
    now=None,
) -> list[Memory]:
    """任务结束时抽取偏好并写入(只在 compose 终态调用,不在对话循环里写)。

    ``session_id`` 默认 None = 长期记忆(跨会话);返回实际写入(或已存在)的行。
    """
    written: list[Memory] = []
    for candidate in extract_candidates(text, llm):
        try:
            row = create_memory(
                session,
                merchant_id=merchant_id,
                user_id=user_id,
                memory_type=str(candidate.get("type") or "preference"),
                text=str(candidate.get("text") or ""),
                source=str(candidate.get("source") or "user_stated"),
                scope=str(candidate.get("scope") or DEFAULT_SCOPE),
                session_id=session_id,
                importance=float(candidate.get("importance") or 0.5),
                now=now,
                executable=candidate.get("executable"),
            )
        except ValueError:
            # 白名单拒绝的候选直接丢弃(真实模型可能给出非法类型/来源)
            continue
        written.append(row)
    return written
