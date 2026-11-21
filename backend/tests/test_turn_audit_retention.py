"""R14 第二半(2026-11-21 企业工程评审核验):turn_audit 的留存口径。

核验查实:`turn_audit.reply` 存**对客回复全文**,无脱敏、无 TTL,
而该表**设计上比业务数据活得久**(模型注释明写「业务数据被删除后仍可留存」)。
回复里若带手机号/地址,就长期留存。

**为什么不用脱敏解决**:审计表的定位就是"事后可查实际说了什么",
脱敏会**摧毁审计价值**。所以正确的杠杆是**留存期**,不是涂掉内容。

**为什么默认不清理**:保留期属合规决策(等保/GDPR/行业监管),
本项目**没有给定合规要求**。在没拍板前自动删审计行是**破坏性且不可逆**的。
所以:能力做出来,但**默认关闭**,必须显式设 `TURN_AUDIT_RETENTION_DAYS` 才生效。
真实取值待合规裁定 —— 不编造合规结论。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import jobs
from app.db import utc_now
from app.models.jobs import TurnAudit


def _audit(engine, *, days_ago: int, reply: str = "已为您登记") -> str:
    with Session(engine) as s:
        row = jobs.write_turn_audit(
            s, trace_id=f"tr-{days_ago}", merchant_id="seed-merchant-1",
            customer_id="seed-cust-001", intent="policy_qa", reply=reply,
        )
        row.created_at = utc_now() - timedelta(days=days_ago)
        s.commit()
        return row.id


def _count(engine) -> int:
    with Session(engine) as s:
        return int(s.scalar(select(func.count()).select_from(TurnAudit)) or 0)


def test_retention_unset_does_not_delete_anything(engine, monkeypatch):
    """**默认关闭**:没设保留期就一行都不删 —— 没拍板前删审计是不可逆破坏。"""
    monkeypatch.delenv("TURN_AUDIT_RETENTION_DAYS", raising=False)
    _audit(engine, days_ago=400)
    _audit(engine, days_ago=1)
    assert jobs.purge_turn_audit(engine) == 0
    assert _count(engine) == 2


def test_retention_purges_only_older_rows(engine, monkeypatch):
    monkeypatch.setenv("TURN_AUDIT_RETENTION_DAYS", "30")
    old = _audit(engine, days_ago=45)
    recent = _audit(engine, days_ago=5)
    deleted = jobs.purge_turn_audit(engine)
    assert deleted == 1
    with Session(engine) as s:
        ids = {r[0] for r in s.execute(select(TurnAudit.id))}
    assert old not in ids
    assert recent in ids


def test_boundary_row_exactly_at_retention_is_kept(engine, monkeypatch):
    """边界:刚好等于保留期的行**保留**(删除口径是"严格早于",不是">=")。"""
    monkeypatch.setenv("TURN_AUDIT_RETENTION_DAYS", "30")
    _audit(engine, days_ago=29)
    assert jobs.purge_turn_audit(engine) == 0
    assert _count(engine) == 1


def test_purge_is_idempotent(engine, monkeypatch):
    monkeypatch.setenv("TURN_AUDIT_RETENTION_DAYS", "30")
    _audit(engine, days_ago=60)
    assert jobs.purge_turn_audit(engine) == 1
    assert jobs.purge_turn_audit(engine) == 0


def test_invalid_retention_is_explicit_error_not_silent_disable(engine, monkeypatch):
    """非法取值必须报错,**不能静默当作未设** —— 那会让人以为配了留存其实没配。"""
    monkeypatch.setenv("TURN_AUDIT_RETENTION_DAYS", "soon")
    _audit(engine, days_ago=400)
    with pytest.raises(RuntimeError):
        jobs.purge_turn_audit(engine)
    assert _count(engine) == 1


def test_non_positive_retention_is_rejected(engine, monkeypatch):
    """0 或负数会删光审计,必须拒绝。"""
    monkeypatch.setenv("TURN_AUDIT_RETENTION_DAYS", "0")
    _audit(engine, days_ago=1)
    with pytest.raises(RuntimeError):
        jobs.purge_turn_audit(engine)
    assert _count(engine) == 1