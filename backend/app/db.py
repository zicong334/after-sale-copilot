"""数据库引擎与会话管理(数据层三铁律:金额整数分、时间 UTC、状态有 CHECK)。

约定:
- SQLite + WAL(PRAGMA journal_mode=WAL)
- 每个连接 PRAGMA foreign_keys=ON(SQLite 外键默认不生效,必须显式开)
- 时间列默认值统一走 ``utc_now()``;TEXT 主键默认值统一走 ``gen_id()``
"""
from __future__ import annotations

import logging
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

logger = logging.getLogger("app.db")


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


def schema_head_revision() -> str:
    """从**迁移链**读当前 head(R15)。刻意不硬编码 —— 硬编码 head 前面已踩过三次。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # 不用 alembic.ini:避免 fileConfig 在测试进程内改日志配置(与 test_services 同口径)
    cfg = Config()
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"迁移链 head 不唯一(分支未合并?): {heads}")
    return str(heads[0])


def assert_schema_at_head(eng: Engine) -> str:
    """校验库的 schema 版本是否等于迁移链 head(R15,2026-11-21 评审核验新增)。

    为什么需要:`main.lifespan` 用 ``create_all`` 兜底建表,**绕过 Alembic**;
    ``create_all`` 只建缺失的表、**从不 ALTER 已存在的表** ——
    这正是 ``dev.db`` 缺 ``order.delivered_at`` 却"看起来能起"的机制原因。
    生产只靠它,schema 会静默漂移于迁移链。

    三档口径(刻意不一律拒绝):
    - 无 ``alembic_version`` 表 → create_all 路径(测试/演示),**放行 + 告警**;
      一律拒绝会打断数百条不带迁移的存量测试。
    - 有且等于 head → ``"ok"``。
    - 有但不等于 head(或版本不在链上)→ **抛 RuntimeError**,拒绝启动。

    与存储无关(用 SQLAlchemy 读表),迁 PG 后同样生效。
    """
    from sqlalchemy import inspect, text

    head = schema_head_revision()
    # schema 感知(2026-11-21 CI 实证):schema_translate_map 只作用于 DDL/查询,
    # **不作用于 Inspector** —— Inspector 默认看 search_path(PG 通常是 public)。
    # 若 engine 被映射到某个 schema(多租户/测试隔离),必须查**那个 schema**,
    # 否则会拿 public 的 alembic_version 误判本 schema 的状态。
    # 生产无映射 → schema=None → 行为一字不变。
    schema = (eng.get_execution_options().get("schema_translate_map") or {}).get(None)
    if not inspect(eng).has_table("alembic_version", schema=schema):
        logger.warning(
            "库无 alembic_version 表(create_all 路径,测试/演示)。"
            "生产必须走 alembic upgrade head —— create_all 不会 ALTER 已存在的表,"
            "schema 可能静默漂移。期望 head=%s", head,
        )
        return "unversioned"
    # 原始 text() SQL **不受 schema_translate_map 影响**(它只翻译带 schema 的 Table 对象),
    # 所以有映射时必须显式限定 schema,否则这条查询仍会打到 search_path(public)。
    qualified = f'"{schema}".alembic_version' if schema else "alembic_version"
    with eng.connect() as conn:
        rows = [str(r[0]) for r in conn.execute(text(f"SELECT version_num FROM {qualified}"))]
    if len(rows) == 1 and rows[0] == head:
        return "ok"
    raise RuntimeError(
        f"schema 版本漂移,拒绝启动:库内版本={rows or '(空)'} 期望 head={head}。"
        f"请执行 alembic upgrade head(迁移只加列不回填数据时,另需按交接说明回填)。"
    )


def _attach_sqlite_pragmas(eng: Engine) -> None:
    """给 engine 绑定 SQLite 连接级 PRAGMA:外键开 + WAL。"""

    @event.listens_for(eng, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def create_db_engine(url: str | None = None) -> Engine:
    """创建 engine;SQLite 附 WAL + foreign_keys=ON,其他后端不附 SQLite PRAGMA。

    R3 前置(2026-11-21):原先无条件挂 SQLite PRAGMA 监听器,
    在 PG 上连接即报语法错 —— 这是"换 URL 就能跑 PG"不成立的另一个原因。
    """
    target_url = url or get_database_url()
    is_sqlite = str(target_url).strip().lower().startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    eng = create_engine(target_url, connect_args=connect_args)
    if is_sqlite:
        _attach_sqlite_pragmas(eng)
    return eng


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
