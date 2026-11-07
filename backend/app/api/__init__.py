"""API 路由包。get_db 依赖函数放这里,避免 main ↔ api 循环导入。"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.db import SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI 依赖:每请求一个 Session,请求结束关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
