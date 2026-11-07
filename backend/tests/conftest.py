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

from app.db import create_db_engine
from app.models import Base


@pytest.fixture()
def tmp_dir():
    """workspace 内的唯一临时目录,结束后清理。

    注意:不用 tempfile.mkdtemp —— 它创建的目录权限受限(0o700),
    在 DSH 沙盒下会导致其内文件无法写入(sqlite "unable to open database file")。
    改用 os.getcwd() 绝对路径 + Path.mkdir()(默认 0o777),与 os.makedirs 一致。
    """
    d = Path(os.getcwd()) / f".w1a_tmp_{uuid.uuid4().hex}"
    d.mkdir()
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
