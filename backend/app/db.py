"""数据库引擎与会话管理(数据层三铁律:金额整数分、时间 UTC、状态有 CHECK)。

约定:
- SQLite + WAL(PRAGMA journal_mode=WAL)
- 每个连接 PRAGMA foreign_keys=ON(SQLite 外键默认不生效,必须显式开)
- 时间列默认值统一走 ``utc_now()``;TEXT 主键默认值统一走 ``gen_id()``
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

# backend/dev.db(相对 backend/ 目录),alembic.ini 里的 url 指向同一位置
_BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _BACKEND_DIR / "dev.db"


def utc_now() -> datetime:
    """统一 UTC 时间戳默认值(数据层三铁律:时间一律 UTC)。"""
    return datetime.now(timezone.utc)


def gen_id() -> str:
    """统一 TEXT 主键生成规则:uuid4 字符串,不用自增裸整数。"""
    return str(uuid4())


def get_database_url() -> str:
    """返回 SQLite 连接串,支持 DATABASE_URL 环境变量覆盖。"""
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


def _attach_sqlite_pragmas(eng: Engine) -> None:
    """给 engine 绑定 SQLite 连接级 PRAGMA:外键开 + WAL。"""

    @event.listens_for(eng, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def create_db_engine(url: str | None = None) -> Engine:
    """创建带 WAL + foreign_keys=ON 的 SQLite engine(测试可传 :memory: 或临时文件)。"""
    target_url = url or get_database_url()
    eng = create_engine(target_url, connect_args={"check_same_thread": False})
    _attach_sqlite_pragmas(eng)
    return eng


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
