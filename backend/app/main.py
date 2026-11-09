"""FastAPI 服务入口:一条命令起服务(/health 200),不接真实 LLM、不发外部网络请求。

启动方式(在 backend 目录下)::

    .venv\\Scripts\\python -m uvicorn app.main:app --reload

- 单 app + lifespan(建 engine,幂等建表 + dispose)
- /health 健康检查
- 静态注册 8 个错误处理器
- 业务路由用 include_router 挂载,不在本文件直接写路由
- 无 key 启动:本文件不 import 任何 LLM / 外部 key / 网络客户端
- html 响应统一 Cache-Control: no-store(W5-FIX R4:僵尸服务缓存曾污染浏览器)
- 内联 SVG favicon(W5-FIX R3:消掉 /favicon.ico 404)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import agent as agent_api
from app.api import chat as chat_api
from app.api import confirm as confirm_api
from app.api import handoff as handoff_api
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


@app.middleware("http")
async def no_store_html(request: Request, call_next):
    """给所有 text/html 响应加 ``Cache-Control: no-store``(W5-FIX R4)。

    CSS/JS 仍走 StaticFiles 的 etag/last-modified 正常缓存;只有页面壳不缓存,
    避免僵尸服务时代的残破 HTML 缓存继续污染用户浏览器。
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


# 内联 SVG favicon(W5-FIX R3):浏览器默认会请求 /favicon.ico,
# 之前落到 StaticFiles 变成 404;两页 HTML 里也内联了 data URI 图标。
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="9" fill="#C05621"/>'
    '<text x="16" y="22.5" font-size="16" text-anchor="middle" fill="#FFFFFF" '
    'font-family="system-ui, sans-serif">客</text>'
    "</svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def favicon() -> Response:
    """品牌色内联 SVG 图标:消掉浏览器 /favicon.ico 404(W5-FIX R3)。"""
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/health")
def health() -> dict:
    """健康检查:返回 200,证明服务无 key 也能起。"""
    return {"ok": True, "status": "healthy"}


app.include_router(orders_api.router)
app.include_router(tickets_api.router)
app.include_router(refunds_api.router)
app.include_router(chat_api.router)
app.include_router(confirm_api.router)
app.include_router(handoff_api.router)
app.include_router(agent_api.router)
app.include_router(memory_maintenance_router)

# 前端聊天壳(原生 HTML/CSS/JS,无构建工具、无 CDN):GET / 或 /chat.html → 200。
# 挂载放最后,避免 "/" 抢在其他 API 路由之前匹配。
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.is_dir():

    @app.get("/", include_in_schema=False)
    def chat_page() -> FileResponse:
        """聊天壳入口:GET / → chat.html(原生页面,无构建工具)。"""
        return FileResponse(_FRONTEND_DIR / "chat.html", media_type="text/html")

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
