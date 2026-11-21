"""测试库 URL 解析(R3 前置,2026-11-21 企业工程评审)。

为什么需要:`conftest.engine` 原先**硬编码 SQLite**,没有覆盖入口 ——
所以"加个 CI service container 就能在 PG 上验"不成立,必须先参数化。

口径:
- 未设 ``TEST_DATABASE_URL`` → tmp_dir 下的 SQLite(**默认行为一字不变**);
- 设了 → 原样返回(可为 ``postgresql+psycopg://…``);
- 空串/纯空白 → 视为未设(CI 里空变量不该把库指向诡异位置)。

``SQLITE_ONLY_TEST_FILES``:绑死 SQLite 内部(直接用 ``sqlite3`` + ``PRAGMA``)的文件。
非 SQLite 后端下**跳过而不是失败**,否则 PG job 会被一堆"本就不适用"的红掩盖真问题。
"""
from __future__ import annotations

import os
from pathlib import Path

#: 直接用 sqlite3 / PRAGMA 断言 SQLite 内部结构的测试文件。
#: 它们测的是"SQLite 迁移与完整性",在 PG 上不适用 —— 跳过,不是失败。
SQLITE_ONLY_TEST_FILES: tuple[str, ...] = (
    "test_delivery.py",
    "test_services.py",
)

TEST_DB_URL_ENV = "TEST_DATABASE_URL"


def is_sqlite_url(url: str) -> bool:
    """该连接串是否 SQLite(用于决定 SQLite 专属测试是否跳过)。"""
    return str(url or "").strip().lower().startswith("sqlite")


def resolve_test_database_url(tmp_dir: Path) -> str:
    """解析本次测试使用的库 URL。默认 SQLite 临时库,可被 env 覆盖到 PG。"""
    override = os.environ.get(TEST_DB_URL_ENV)
    if override and override.strip():
        return override.strip()
    return f"sqlite:///{(Path(tmp_dir) / 'test.db').as_posix()}"