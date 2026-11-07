"""记忆层:memory 表 + 白名单 + 写入/查询 + 注入 + 衰减。

``import app.memory`` 会把 ``Memory`` 注册进 ``Base.metadata``(alembic 与测试建表
都依赖这一点);不修改 ``app/models/__init__.py``(不在本任务白名单)。
"""
from __future__ import annotations

from app.memory.models import Memory

__all__ = ["Memory"]
