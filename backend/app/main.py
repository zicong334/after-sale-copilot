"""FastAPI 服务入口:一条命令起服务(/health 200),不接真实 LLM、不发外部网络请求。

启动方式(在 backend 目录下)::

    .venv\\\\Scripts\\\\python -m uvicorn app.main:app --reload

- 单 app + lifespan(建 engine,幂等建表 + dispose)
- /health 健康检查
- 静态注册 8 个错误处理器
- 业务路由用 include_router 挂载,不在本文件直接写路由
- 无 key 启动:本文件不 import 任何 LLM / 外部 key / 网络客户端
- html 响应统一 Cache-Control: no-store(W5-FIX R4:僵尸服务缓存曾污染浏览器)
- 内联 SVG favicon(W5-FIX R3:消掉 /favicon.ico 404)
- **W8**:JWT 鉴权中间件(纯 ASGI,不影响 SSE 流式)+ CORS 白名单 +
  WebSocket 双向通道(/ws/buyer、/ws/agent;旧轮询接口保留为断线降级)
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import security
from app.api import agent as agent_api
from app.api import auth as auth_api
from app.api import chat as chat_api
from app.api import confirm as confirm_api
from app.api import handoff as handoff_api
from app.api import orders as orders_api
from app.api import refunds as refunds_api
from app.api import tickets as tickets_api
from app.db import engine, utc_now
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


# ---------------------------------------------------------------------------
# W8:JWT 鉴权中间件(纯 ASGI 实现,不用 BaseHTTPMiddleware,避免影响 SSE 流式)
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """``/api/c/*`` 只允许 buyer token,``/api/b/*`` 只允许 agent token。

    身份统一写入 ``scope["state"]["identity"]``(``request.state.identity``),
    路由一律用 ``Depends(current_identity)`` 取,**不再从请求体/query 读身份**。
    ``AUTH_MODE=internal`` 仅给回归脚本(check_w1_cases.py / demo_w2.py)用,
    跳过鉴权并打 WARN;对外服务必须保持默认 ``enforce``。
    """

    def __init__(self, app) -> None:  # noqa: A002 - ASGI 约定参数名
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # CORS 预检不带 Authorization,直接放行给 CORSMiddleware 处理。
        if scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        required = security.required_role(path)
        if required is None:
            return await self.app(scope, receive, send)

        state = scope.setdefault("state", {})
        if security.auth_mode() == security.AUTH_MODE_INTERNAL:
            state["identity"] = security.Identity(
                sub="internal", role="internal", merchant_id="*"
            )
            return await self.app(scope, receive, send)

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        token = security.bearer_token(headers.get("authorization"))
        if not token:
            return await self._reject(
                scope, receive, send, 401, "AUTH_REQUIRED", "缺少 Bearer token(请先登录)"
            )
        try:
            identity = security.decode_token(token)
        except security.TokenError as exc:
            return await self._reject(scope, receive, send, 401, exc.code, exc.message)
        if required != security.ROLE_ANY and identity.role != required:
            return await self._reject(
                scope,
                receive,
                send,
                403,
                "FORBIDDEN",
                f"该接口需要 {required} 角色,当前角色为 {identity.role}",
            )
        retry_after = security.check_rate_limit(identity)
        if retry_after is not None:
            response = security.error_response(
                429, "RATE_LIMITED", "请求过于频繁,请稍后重试", retryable=True
            )
            response.headers["Retry-After"] = str(max(1, int(round(retry_after))))
            await response(scope, receive, send)
            return
        state["identity"] = identity
        return await self.app(scope, receive, send)

    async def _reject(self, scope, receive, send, status: int, code: str, message: str) -> None:
        response = security.error_response(status, code, message)
        if status == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        await response(scope, receive, send)


app.add_middleware(AuthMiddleware)

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
app.include_router(auth_api.router)
app.include_router(chat_api.router)
app.include_router(confirm_api.router)
app.include_router(handoff_api.router)
app.include_router(agent_api.router)
app.include_router(memory_maintenance_router)


# ---------------------------------------------------------------------------
# W8:WebSocket 双向通道(/ws/buyer、/ws/agent)
# ---------------------------------------------------------------------------


async def _ws_send_loop(websocket: WebSocket, queue: "asyncio.Queue[dict]") -> None:
    """把 broker 事件持续推给客户端(阻塞直到连接断开)。"""
    while True:
        event = await queue.get()
        await websocket.send_json(event)


async def _ws_receive_loop(websocket: WebSocket) -> None:
    """收客户端帧:目前只处理 ``ping``(心跳),其余忽略(协议留扩展位)。"""
    while True:
        message = await websocket.receive_json()
        if isinstance(message, dict) and message.get("type") == "ping":
            await websocket.send_json({"type": "pong", "ts": utc_now().isoformat()})


async def _ws_authenticate(websocket: WebSocket) -> str | None:
    """取 token:优先 ``?token=``;否则等首帧 ``{"type":"auth","token":"..."}``(5 秒)。"""
    token = websocket.query_params.get("token")
    if token:
        return token
    try:
        first = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        return None
    if not isinstance(first, dict) or first.get("type") != "auth":
        return None
    return str(first.get("token") or "") or None


async def _serve_websocket(websocket: WebSocket, *, role: str, target: str | None) -> None:
    """WS 端点公共流程:首帧/查询参数鉴权 → 角色与频道校验 → 订阅 → 双向泵。

    频道:buyer 订阅 ``buyer:{session_id}``;agent 订阅 ``agent:{service_id}`` +
    ``queue:{merchant_id}``(工作台队列实时刷新)。关闭码:4400 参数缺失 /
    4401 未认证 / 4403 角色或归属不符。
    """
    await websocket.accept()
    token = await _ws_authenticate(websocket)
    if not token:
        await websocket.close(code=4401)
        return
    try:
        identity = security.decode_token(token)
    except security.TokenError:
        await websocket.close(code=4401)
        return
    if identity.role != role:
        await websocket.close(code=4403)
        return

    if role == "buyer":
        session_id = target or websocket.query_params.get("session_id")
        if not session_id:
            await websocket.close(code=4400)
            return
        channels = [security.channel_buyer(session_id)]
    else:
        service_id = (
            target or websocket.query_params.get("service_id") or identity.service_id
        )
        if not service_id or service_id != identity.service_id:
            await websocket.close(code=4403)
            return
        channels = [
            security.channel_agent(service_id),
            security.channel_queue(identity.merchant_id),
        ]

    await websocket.send_json(
        {"type": "ready", "role": role, "channels": channels, "ts": utc_now().isoformat()}
    )
    try:
        async with security.BROKER.subscribe(channels) as queue:
            sender = asyncio.create_task(_ws_send_loop(websocket, queue))
            receiver = asyncio.create_task(_ws_receive_loop(websocket))
            try:
                await asyncio.wait(
                    {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (sender, receiver):
                    task.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)
    except (WebSocketDisconnect, asyncio.CancelledError):
        # 客户端断开时 Starlette/TestClient 可能先取消任务再投递 disconnect 帧,
        # 两种收尾都算正常关闭,不向上抛(否则 uvicorn 会记一条无意义 traceback)。
        pass


@app.websocket("/ws/buyer")
@app.websocket("/ws/buyer/{session_id}")
async def ws_buyer(websocket: WebSocket, session_id: str | None = None) -> None:
    """买家通道:坐席回复、工单状态、确认卡状态实时推送。"""
    await _serve_websocket(websocket, role="buyer", target=session_id)


@app.websocket("/ws/agent")
@app.websocket("/ws/agent/{service_id}")
async def ws_agent(websocket: WebSocket, service_id: str | None = None) -> None:
    """坐席通道:工单状态 + 队列新增实时推送(工作台队列实时刷新)。"""
    await _serve_websocket(websocket, role="agent", target=service_id)


# 前端聊天壳(原生 HTML/CSS/JS,无构建工具、无 CDN):GET / 或 /chat.html → 200。
# 挂载放最后,避免 "/" 抢在其他 API 路由之前匹配。
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.is_dir():

    @app.get("/", include_in_schema=False)
    def chat_page() -> FileResponse:
        """聊天壳入口:GET / → chat.html(原生页面,无构建工具)。"""
        return FileResponse(_FRONTEND_DIR / "chat.html", media_type="text/html")

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")


# CORS 白名单:必须最后 add(Starlette 后加的是外层),让 401/429 也带 CORS 头。
# 生产缝:env ``CORS_ORIGINS`` 逗号分隔;任何 ``*`` 都会被 security.cors_origins() 剔除。
app.add_middleware(
    CORSMiddleware,
    allow_origins=security.cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Timestamp", "X-Nonce"],
    expose_headers=["Retry-After"],
    max_age=600,
)
