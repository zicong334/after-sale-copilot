#!/usr/bin/env python
"""rollback.py — 数据库回滚:备份当前 SQLite -> 恢复到指定 Alembic 版本 -> 校验。

为什么需要它
------------
``alembic downgrade`` 本身不备份;回滚失败(迁移脚本缺 downgrade、数据不兼容)时
原库已被改坏。本脚本把三件事固定成一个可重复动作:

1. **备份**:用 SQLite 官方 backup API 复制当前库到 ``.tmp/rollback/``(含 WAL 内容);
2. **回滚**:用 Alembic Python API 执行 ``downgrade <target>``(等价于
   ``python -m alembic -c backend/alembic.ini downgrade <target>``,但工作目录无关);
3. **校验**:回读 ``alembic_version``、``PRAGMA integrity_check``、
   ``PRAGMA foreign_key_check``,三项都过才算成功(退出码 0)。

用法(在项目根执行)::

    # 回滚到 0003_ticket_summary(0004_memory 之前)
    .venv/Scripts/python backend/scripts/rollback.py --target 0003

    # 精确 revision 或唯一前缀都可以;base = 回滚到空库
    .venv/Scripts/python backend/scripts/rollback.py --target 0003_ticket_summary

    # 升级辅助(演练/恢复用),默认 head
    .venv/Scripts/python backend/scripts/rollback.py --upgrade
    .venv/Scripts/python backend/scripts/rollback.py --upgrade --target 0003

    # 查看版本链;指定库文件(默认 backend/dev.db,也可用 DATABASE_URL)
    .venv/Scripts/python backend/scripts/rollback.py --list
    .venv/Scripts/python backend/scripts/rollback.py --target 0002 --db-url sqlite:///backend/dev.db
    .venv/Scripts/python backend/scripts/rollback.py --target 0003 --backup-dir .tmp/rollback

注意
----
- 只支持 SQLite(WAL);非 SQLite URL 会直接报错,不猜。
- 回滚不会自动删备份;确认无误后自行清理 ``.tmp/rollback/``。
- 回滚后如需旧数据,重新 ``--upgrade`` 不会恢复被 downgrade 删除的表数据,
  应从备份文件恢复。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
ALEMBIC_INI = BACKEND / "alembic.ini"
DEFAULT_BACKUP_DIR = ROOT / ".tmp" / "rollback"
DEFAULT_DB_URL = f"sqlite:///{(BACKEND / 'dev.db').as_posix()}"

# 让 ``from alembic...`` 与 env.py 的 ``from app.models import Base`` 都可用。
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


# ---------------------------------------------------------------------------
# URL / 路径
# ---------------------------------------------------------------------------

def resolve_db_url(db_url: str | None = None) -> str:
    """解析并规范化数据库 URL:显式参数 > DATABASE_URL > backend/dev.db。

    SQLite 相对路径统一按项目根解析并转绝对路径,避免"从哪个目录运行"影响结果。
    """
    raw = db_url or os.environ.get("DATABASE_URL") or DEFAULT_DB_URL
    url = make_url(raw)
    if url.get_backend_name() != "sqlite":
        raise SystemExit(
            f"[rollback] 只支持 SQLite,当前 URL={url.render_as_string(hide_password=False)}"
        )
    if not url.database or url.database == ":memory:":
        return "sqlite:///:memory:"
    path = Path(url.database)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    # 故意返回未百分号编码的 URL:alembic 用 configparser 存 URL,含 '%' 会触发
    # interpolation 报错;SQLAlchemy 本身接受中文/空格的原始路径。
    return f"sqlite:///{path.as_posix()}"


def sqlite_path(db_url: str | None = None) -> Path:
    """返回 SQLite 文件绝对路径(供备份/PRAGMA 校验用)。"""
    url = make_url(resolve_db_url(db_url))
    if not url.database or url.database == ":memory:":
        raise SystemExit("[rollback] 内存数据库无法备份/校验,请使用文件型 SQLite")
    return Path(url.database)


# ---------------------------------------------------------------------------
# Alembic 调用
# ---------------------------------------------------------------------------

def _alembic_config(url: str) -> Config:
    """构造不依赖 cwd 的 Alembic Config(script_location 用绝对路径)。"""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@contextmanager
def _database_url_env(url: str) -> Iterator[None]:
    """临时把 DATABASE_URL 指向目标库(env.py 以它覆盖 ini),结束后还原。"""
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


def resolve_revision(target: str) -> str:
    """把 ``0003``/``0003_ticket_summary``/``head``/``base`` 解析成 Alembic revision。"""
    if target in {"head", "base"}:
        return target
    script = ScriptDirectory.from_config(_alembic_config(resolve_db_url()))
    revisions = [rev.revision for rev in script.walk_revisions()]
    if target in revisions:
        return target
    matches = [rev for rev in revisions if rev.startswith(target)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(
            f"[rollback] 未知目标版本 {target!r};可选:{', '.join(revisions)}"
        )
    raise SystemExit(f"[rollback] 前缀 {target!r} 命中多个版本:{', '.join(matches)}")


def list_revisions() -> list[dict[str, str | None]]:
    """列出迁移链(从新到旧)。"""
    script = ScriptDirectory.from_config(_alembic_config(resolve_db_url()))
    return [
        {"revision": rev.revision, "down_revision": rev.down_revision}
        for rev in script.walk_revisions()
    ]


# ---------------------------------------------------------------------------
# 备份 / 校验
# ---------------------------------------------------------------------------

def backup_database(
    db_url: str | None = None, backup_dir: Path | None = None
) -> Path:
    """用 SQLite backup API 备份当前库,返回备份文件路径。"""
    source = sqlite_path(db_url)
    if not source.is_file():
        raise SystemExit(f"[rollback] 数据库不存在:{source}")
    target_dir = Path(backup_dir or DEFAULT_BACKUP_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = target_dir / f"{source.stem}.{stamp}.db"
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dst:
        src.backup(dst)
    return target


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def verify_database(
    db_url: str | None = None, expected_revision: str | None = None
) -> dict[str, Any]:
    """回读版本号 + integrity_check + foreign_key_check。"""
    path = sqlite_path(db_url)
    if not path.is_file():
        raise SystemExit(f"[rollback] 数据库不存在:{path}")
    with sqlite3.connect(str(path)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        current: str | None = None
        if _table_exists(conn, "alembic_version"):
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            current = row[0] if row else None
    result = {
        "db_path": str(path),
        "current_revision": current,
        "expected_revision": expected_revision,
        "integrity_check": integrity,
        "foreign_key_violations": len(fk_rows),
        "ok": integrity == "ok"
        and not fk_rows
        and (expected_revision is None or current == expected_revision),
    }
    return result


# ---------------------------------------------------------------------------
# 对外动作
# ---------------------------------------------------------------------------

def _expected_revision(revision: str) -> str | None:
    """把 'head' 展开为当前唯一 head 的 revision;base 表示无版本。"""
    if revision == "base":
        return None
    if revision == "head":
        script = ScriptDirectory.from_config(_alembic_config(resolve_db_url()))
        return script.get_current_head()
    return revision


def upgrade_database(db_url: str | None = None, target: str = "head") -> dict[str, Any]:
    """升级到指定版本(默认 head),返回校验结果。"""
    url = resolve_db_url(db_url)
    revision = resolve_revision(target)
    with _database_url_env(url):
        command.upgrade(_alembic_config(url), revision)
    result = verify_database(url, expected_revision=_expected_revision(revision))
    if not result["ok"]:
        raise SystemExit(f"[rollback] 升级后校验失败:{json.dumps(result, ensure_ascii=False)}")
    return result


def downgrade_database(
    db_url: str | None = None,
    target: str | None = None,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """备份 -> downgrade 到 target -> 三项校验,返回 {backup, ...校验结果}。"""
    if not target:
        raise SystemExit("[rollback] downgrade 必须指定 --target")
    url = resolve_db_url(db_url)
    revision = resolve_revision(target)
    backup = backup_database(url, backup_dir=backup_dir)
    with _database_url_env(url):
        command.downgrade(_alembic_config(url), revision)
    result = verify_database(url, expected_revision=_expected_revision(revision))
    result["backup"] = str(backup)
    result["target"] = revision
    if not result["ok"]:
        raise SystemExit(
            f"[rollback] 回滚后校验失败(备份在 {backup}):"
            f"{json.dumps(result, ensure_ascii=False)}"
        )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="售后 Copilot 数据库回滚(备份 -> Alembic downgrade -> 校验)"
    )
    parser.add_argument("--target", help="目标版本(revision 或唯一前缀,如 0003);base=空库")
    parser.add_argument(
        "--upgrade",
        nargs="?",
        const="head",
        metavar="REV",
        help="升级到 REV(省略 REV 时为 head)",
    )
    parser.add_argument("--db-url", default=None, help="数据库 URL(默认 backend/dev.db)")
    parser.add_argument(
        "--backup-dir", default=None, help="备份目录(默认 .tmp/rollback)"
    )
    parser.add_argument("--list", action="store_true", help="列出迁移版本链")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list:
        for item in list_revisions():
            print(f"{item['revision']}  <- {item['down_revision']}")
        return 0
    if args.upgrade and args.target:
        raise SystemExit("[rollback] --upgrade 与 --target 不能同时使用")
    if not args.upgrade and not args.target:
        raise SystemExit("[rollback] 需要 --target(回滚)或 --upgrade(升级)或 --list")

    url = resolve_db_url(args.db_url)
    print(f"[rollback] 数据库:{url}")
    if args.upgrade:
        result = upgrade_database(url, target=args.upgrade)
        print(f"[rollback] 已升级到 {result['current_revision']}")
    else:
        backup_dir = Path(args.backup_dir) if args.backup_dir else None
        result = downgrade_database(url, target=args.target, backup_dir=backup_dir)
        print(f"[rollback] 已备份:{result['backup']}")
        print(f"[rollback] 已回滚到 {result['current_revision']}")
    print(
        "[rollback] 校验:"
        f" integrity_check={result['integrity_check']},"
        f" foreign_key_violations={result['foreign_key_violations']},"
        f" current_revision={result['current_revision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
