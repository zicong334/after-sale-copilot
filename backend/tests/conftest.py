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
from app.db import create_db_engine
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
    security.reset_security_state()
    yield
    security.reset_security_state()


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
    """每个测试独立 schema 的 SQLite engine(WAL + foreign_keys=ON 已内置)。"""
    eng = create_db_engine(f"sqlite:///{(tmp_dir / 'test.db').as_posix()}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    """独立事务的 Session;测试自行 commit/rollback,结束后由 engine dispose 清理。"""
    with Session(engine) as sess:
        yield sess
