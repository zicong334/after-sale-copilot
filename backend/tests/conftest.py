"""pytest fixtures:每个测试独立临时文件 DB + Session(约束靠数据库,不靠 Python 自觉)。"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

# 把 backend/ 加入 sys.path,保证 `from app...` 可导入(不依赖 pytest 配置)
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import pytest
from sqlalchemy.orm import Session

from app import security
from app.cache import semantic_cache
from app.db import create_db_engine
from app.llm import reset_shared_breakers
from app.models import Base


# ---------------------------------------------------------------------------
# W8:既有测试的兼容边界(autouse)
# ---------------------------------------------------------------------------
#
# 为什么必须是 autouse:W8 之前已有 12 个 TestClient 测试文件(W1-W7 的 434 条
# 用例),它们的调用方式与安全基建天然冲突——例如毫秒级连发同一接口(会触发
# 用户级 5 QPS/突发 10 的令牌桶)、写接口只带 body 不带 X-Timestamp/X-Nonce
# (会被防重放依赖 400)、chat 请求不带 JWT(会被鉴权中间件 401)。这些用例是
# 回归红线,既不能删断言也不能放宽断言,因此**测试环境默认回到内部模式**:
#
# - ``AUTH_MODE=internal``:中间件放行并写入 internal 身份,路由经
#   ``security.effective_identity`` 从请求体兜底身份(与 W8 前行为一致);
# - ``RATE_LIMIT_ENABLED=False``:关闭令牌桶,毫秒级连发不因限流变 429;
# - ``REPLAY_GUARD_ENABLED=False``:关闭防重放,既有写请求不必补两个头。
#
# 新安全测试(test_auth/test_ratelimit/test_replay/test_ws)在自己文件里用
# ``monkeypatch.setenv("AUTH_MODE","enforce")`` + 显式打开开关并
# ``security.reset_security_state()`` 覆盖本 fixture,从而测的仍是生产模式。
# 无论哪种模式,每个用例前后都重置进程内安全状态(限流桶 / PubSub 订阅表),
# 避免用例之间互相污染(单进程内存实现,串行执行)。
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def w8_security_defaults(monkeypatch):
    """所有测试默认 internal 模式 + 关限流/防重放;前后重置安全状态。"""
    monkeypatch.setenv("AUTH_MODE", security.AUTH_MODE_INTERNAL)
    monkeypatch.setattr(security, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(security, "REPLAY_GUARD_ENABLED", False)
    # W11-3:SLA 后台扫描线程在测试里默认关闭(假时钟用例直接调 scan_breaches),
    # 避免每个 TestClient 生命周期都拉起守护线程、跨用例写库。
    monkeypatch.setenv("SLA_SCHEDULER_ENABLED", "0")
    security.reset_security_state()
    # W13:SemanticCache 是进程级单例,chat 读路径接入后必须逐用例清空,
    # 否则前一个用例的问答会被后一个用例命中(跨用例污染)。
    semantic_cache.clear()
    # F4:模型网关的两个熔断器也是进程级共享(使熔断状态跨请求存活),
    # 同样必须逐用例重置,否则前一个用例的 open 状态会污染后一个用例。
    reset_shared_breakers()
    yield
    semantic_cache.clear()
    security.reset_security_state()
    reset_shared_breakers()


@pytest.fixture()
def tmp_dir():
    """workspace 内的唯一临时目录,结束后清理。

    注意:不用 tempfile.mkdtemp —— 它创建的目录权限受限(0o700),
    在 DSH 沙盒下会导致其内文件无法写入(sqlite "unable to open database file")。
    改用 os.getcwd() 绝对路径 + Path.mkdir()(默认 0o777),与 os.makedirs 一致。
    位置统一放 .tmp/ 下(已 gitignore):会话中途被取消时残留也不污染项目根
    (2026-11-07 主会话维护性修改,W1-A 原版建在根目录 .w1a_tmp_*)。
    """
    d = Path(os.getcwd()) / ".tmp" / f"w1a_{uuid.uuid4().hex}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def engine(tmp_dir):
    """每个测试**真正独立**的 schema。

    SQLite:tmp_dir 下的独立文件(天然隔离,行为一字不变)。
    PG:**必须每用例建独立 schema** —— 2026-11-21 CI 实证:第一版参数化只是把 URL 换成 PG,
    962 个用例**共用同一个 schema**,seed() 反复 DELETE+INSERT 叠加残留 job worker 持事务
    → 锁竞争/死锁 → **PG 全量步骤卡死 121 分钟**(SQLite 同套用例只要 104 秒)。
    docstring 当时写着"独立 schema"但 PG 并没有,是**名不副实**,这里补齐。

    实现用 SQLAlchemy 的 ``schema_translate_map``:把默认 schema(None)映射到本用例专属 schema,
    create_all 与所有 ORM 查询都落在该 schema 内;teardown DROP SCHEMA CASCADE。
    """
    import uuid

    from sqlalchemy import text

    from tests.db_url_helper import is_sqlite_url, resolve_test_database_url

    url = resolve_test_database_url(tmp_dir)
    if is_sqlite_url(url):
        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
        try:
            yield eng
        finally:
            eng.dispose()
        return

    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = create_db_engine(url)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()
    # PG 与 SQLite 的**锁语义差异**(2026-11-21 CI 具名堆栈实证):
    # test_jobs.py 的 test_log_table fixture 在 teardown 里 drop_all,
    # 注释写着"调用方 session 仍持有读事务时 SQLite 会报 database is locked",并用 except 吞掉。
    # **但 PG 上 DROP TABLE 不报错,它无限等 ACCESS EXCLUSIVE 锁** → except 永不触发 → 卡死。
    # 三个超时把"无限等"变成"快速失败",让既有 except 兜底按设计生效:
    #   lock_timeout          DDL/行锁等不到就报错(而不是挂住)
    #   idle_in_transaction_session_timeout  **掐掉持事务空转的连接**(正是持锁者)
    #   statement_timeout     单条语句上限,防任何意外长阻塞
    from sqlalchemy import create_engine

    pg_engine = create_engine(
        url,
        connect_args={
            "options": (
                "-c lock_timeout=3000 "
                "-c idle_in_transaction_session_timeout=10000 "
                "-c statement_timeout=60000"
            )
        },
    )
    eng = pg_engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(eng)
        yield eng
    finally:
        eng.dispose()
        cleanup = create_db_engine(url)
        try:
            with cleanup.begin() as conn:
                conn.execute(text("SET lock_timeout = '3s'"))
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        except Exception:  # noqa: BLE001 - CI 用一次性 PG,残留 schema 无害;不得因此挂住
            pass
        finally:
            cleanup.dispose()


@pytest.fixture()
def session(engine):
    """独立事务的 Session;测试自行 commit/rollback,结束后由 engine dispose 清理。"""
    with Session(engine) as sess:
        yield sess


def pytest_collection_modifyitems(config, items):
    """R3 前置:非 SQLite 后端下**跳过**绑死 SQLite 内部的测试文件。

    `test_delivery.py` / `test_services.py` 直接用 sqlite3 + PRAGMA 断言
    SQLite 迁移与完整性,在 PG 上**不适用**。跳过而不是失败 ——
    否则 PG job 会被一堆"本就不适用"的红掩盖真问题。
    默认 SQLite 路径下本钩子不做任何事(存量行为一字不变)。
    """
    from tests.db_url_helper import (
        SQLITE_ONLY_TEST_FILES,
        is_sqlite_url,
        resolve_test_database_url,
    )

    url = resolve_test_database_url(Path(config.rootpath) / ".tmp")
    if is_sqlite_url(url):
        return
    skip = pytest.mark.skip(reason=f"SQLite 专属断言,当前后端={url.split(':')[0]}")
    for item in items:
        if Path(str(item.fspath)).name in SQLITE_ONLY_TEST_FILES:
            item.add_marker(skip)
