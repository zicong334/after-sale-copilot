"""R3 前置(2026-11-21):测试库 URL 必须可切换,否则 PG 上根本跑不起来。

评审核验的三前提之一:`conftest.engine` **硬编码 SQLite**
(`create_db_engine(f"sqlite:///{tmp_dir}/test.db")`),没有覆盖入口 ——
所以"加个 CI service container 就能在 PG 上验"是不成立的,得先参数化。

口径:
- 未设 `TEST_DATABASE_URL` → 仍走 tmp_dir 下的 SQLite(**默认行为一字不变**,915 条存量不受影响);
- 设了 → 原样返回(可以是 postgresql+psycopg://…);
- 空串/纯空白 → 视为未设(避免 CI 里空变量把库指向诡异位置)。

另有 2 个文件**绑死 SQLite 内部**(直接用 sqlite3 + PRAGMA):
`test_delivery.py` / `test_services.py`。它们在非 SQLite 后端下必须**跳过而不是失败**,
否则 PG job 会被一堆"本就不适用"的红掩盖真问题。
"""
from __future__ import annotations

from pathlib import Path

from tests.db_url_helper import is_sqlite_url, resolve_test_database_url


def test_default_is_sqlite_under_tmp_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    url = resolve_test_database_url(tmp_path)
    assert url.startswith("sqlite:///")
    assert Path(tmp_path).as_posix() in url


def test_env_override_is_returned_verbatim(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/copilot")
    assert resolve_test_database_url(tmp_path) == "postgresql+psycopg://u:p@db:5432/copilot"


def test_blank_env_is_treated_as_unset(monkeypatch, tmp_path):
    """空串/空白视为未设 —— CI 里空变量不该把库指向诡异位置。"""
    for blank in ("", "   ", "\t"):
        monkeypatch.setenv("TEST_DATABASE_URL", blank)
        assert resolve_test_database_url(tmp_path).startswith("sqlite:///")


def test_is_sqlite_url_classification():
    assert is_sqlite_url("sqlite:///x/y.db") is True
    assert is_sqlite_url("sqlite://") is True
    assert is_sqlite_url("postgresql+psycopg://u:p@h/db") is False
    assert is_sqlite_url("postgresql://u:p@h/db") is False


def test_sqlite_bound_test_files_are_declared():
    """绑死 SQLite 内部的两个文件必须被显式登记,PG job 才能跳过它们。"""
    from tests.db_url_helper import SQLITE_ONLY_TEST_FILES

    assert "test_delivery.py" in SQLITE_ONLY_TEST_FILES
    assert "test_services.py" in SQLITE_ONLY_TEST_FILES