"""FastAPI 服务入口:一条命令起服务(/health 200),不接真实 LLM、不发外部网络请求。

启动方式(在 backend 目录下)::

    .venv\\Scripts\\python -m uvicorn app.main:app --reload

- 单 app + lifespan(建 engine,幂等建表 + dispose)
- /health 健康检查
- 静态注册 8 个错误处理器
- 业务路由用 include_router 挂载,不在本文件直接写路由
- 无 key 启动:本文件不 import 任何 LLM / 外部 key / 网络客户端
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import chat as chat_api
from app.api import orders as orders_api
from app.api import refunds as refunds_api
from app.api import tickets as tickets_api
from app.db import engine
from app.errors import register_error_handlers
from app.memory.decay import router as memory_maintenance_router
from app.models import Base


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时幂等建表(dev.db 已由 alembic 建好,此处兜底无迁移环境也能用);退出时 dispose。"""
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


app = FastAPI(title="售后 Copilot 服务", lifespan=lifespan)

register_error_handlers(app)


@app.get("/health")
def health() -> dict:
    """健康检查:返回 200,证明服务无 key 也能起。"""
    return {"ok": True, "status": "healthy"}


app.include_router(orders_api.router)
app.include_router(tickets_api.router)
app.include_router(refunds_api.router)
app.include_router(chat_api.router)
app.include_router(memory_maintenance_router)
