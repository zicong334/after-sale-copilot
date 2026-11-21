"""R15(2026-11-21 评审核验新增):启动必须校验 schema 版本,不能靠 create_all 兜底。

机制根因:`main.lifespan` 用 `Base.metadata.create_all(bind=engine)` 兜底建表,
**绕过 Alembic**。`create_all` 只建**缺失的表**,**从不 ALTER 已存在的表** ——
这正是上一批 F1 里 `backend/dev.db` 缺 `order.delivered_at` 却"看起来能起"的原因:
表在、列不在,create_all 认为一切正常。

生产若只靠 create_all,**schema 会静默漂移于迁移链**。

口径(刻意分三档,不是一律拒绝):
- `alembic_version` 表**不存在** → 这是 create_all 路径(测试/演示),**放行 + 告警**;
  否则数百条不带迁移的存量测试会全断。
- 表存在且**等于 head** → 放行。
- 表存在但**不等于 head** → **拒绝启动**并给明确错误(这才是真实漂移)。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import assert_schema_at_head, schema_head_revision


def _stamp(engine, revision: str) -> None:
    """手工建 alembic_version 并写入指定版本(模拟已迁移过的库)。"""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": revision})


def test_head_revision_comes_from_migration_chain_not_hardcoded():
    """head 必须从迁移链读,不能硬编码 —— 否则加迁移又要改一处断言(前面已踩过三次)。"""
    head = schema_head_revision()
    assert head
    assert head.startswith("0011_"), f"当前链头应是 0011,实际 {head}"


def test_unversioned_create_all_db_is_allowed_with_warning(engine, caplog):
    """create_all 建的库没有 alembic_version → 放行(测试/演示路径),但要告警。"""
    with Session(engine) as s:
        assert s.execute(text("SELECT name FROM sqlite_master WHERE name='alembic_version'")).first() is None
    assert assert_schema_at_head(engine) == "unversioned"


def test_db_stamped_at_head_is_ok(engine):
    _stamp(engine, schema_head_revision())
    assert assert_schema_at_head(engine) == "ok"


def test_db_behind_head_is_rejected(engine):
    """真实漂移:库停在旧版本 → 拒绝启动,错误里要同时给出当前与期望版本。"""
    _stamp(engine, "0009_fulfillment")
    with pytest.raises(RuntimeError) as ei:
        assert_schema_at_head(engine)
    msg = str(ei.value)
    assert "0009_fulfillment" in msg
    assert schema_head_revision() in msg


def test_unknown_revision_is_rejected(engine):
    """库里是个链上不存在的版本(如别的分支残留)→ 同样拒绝,不能当 ok。"""
    _stamp(engine, "9999_from_other_branch")
    with pytest.raises(RuntimeError):
        assert_schema_at_head(engine)