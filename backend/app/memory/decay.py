"""记忆衰减:公式照记忆系统设计 ``decayer.py``(importance 保护 + 指数衰减 + 宽限过期),
对外暴露 ``POST /v1/maintenance/decay`` 供定时任务/调度器调用。

规则(默认参数与参考实现一致):
- 候选:``importance <= IMPORTANCE_PROTECT`` 且 idle >= ``DECAY_WINDOW_DAYS``;
- 衰减:``decay_score *= FACTOR``(本表把参考实现的 importance 落成 decay_score,
  importance 只作为保护权重,避免"用户声明的偏好"被误删);
- 过期:TTL 到期,或 ``decay_score < DELETE_THRESHOLD`` 且 idle >= ``GRACE_DAYS``;
- 只处理未软删的行;quarantined 行只做过期,不参与衰减。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.memory.models import Memory

#: 进入衰减的最短闲置天数
DECAY_WINDOW_DAYS: int = 30
#: 低分记忆进入过期的宽限天数
GRACE_DAYS: int = 90
#: 每次衰减的乘数
DECAY_FACTOR: float = 0.9
#: importance 高于该值的记忆不参与衰减
IMPORTANCE_PROTECT: float = 0.7
#: decay_score 低于该值的记忆(过宽限期后)标记过期
DELETE_THRESHOLD: float = 0.2


def _as_utc(value: datetime) -> datetime:
    """SQLite 读回的时间是 naive(按 UTC 存);统一补 UTC 时区再比较。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def decay(session: Session, *, now: datetime | None = None) -> dict[str, int]:
    """执行一轮衰减,返回 ``{"decayed": 衰减条数, "expired": 过期条数}``。"""
    now = now or utc_now()
    rows = list(
        session.scalars(
            select(Memory).where(
                Memory.deleted_at.is_(None),
                Memory.status.in_(("active", "quarantined")),
            )
        )
    )
    decayed = 0
    expired = 0
    for row in rows:
        # 1) TTL 到期:active/quarantined 都直接标 expired(不物理删除,用户可查)
        if row.ttl_expires_at is not None and _as_utc(row.ttl_expires_at) <= now:
            row.status = "expired"
            expired += 1
            continue
        # 2) 隔离项只等 TTL,不参与衰减
        if row.status != "active":
            continue
        if row.importance > IMPORTANCE_PROTECT:
            continue
        idle_days = int((now - _as_utc(row.created_at)).total_seconds() // 86400)
        if idle_days >= DECAY_WINDOW_DAYS:
            row.decay_score = round(max(0.0, float(row.decay_score) * DECAY_FACTOR), 4)
            decayed += 1
        if row.decay_score < DELETE_THRESHOLD and idle_days >= GRACE_DAYS:
            row.status = "expired"
            expired += 1
    session.flush()
    return {"decayed": decayed, "expired": expired}


router = APIRouter(prefix="/v1/maintenance", tags=["maintenance"])


@router.post("/decay")
def run_decay(db: Session = Depends(get_db)) -> dict[str, int]:
    """衰减调度入口:定时任务(或手动 curl)调用一次跑一轮。"""
    result = decay(db)
    db.commit()
    return result
