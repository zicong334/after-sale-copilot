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
from contextlib import asynccontextmanager, contextmanager
import logging
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import jobs
from app import observability
from app import security
from app import sla
from app.api import agent as agent_api
from app.api import badcases as badcases_api
from app.api import get_db
from app.api import kb as kb_api
from app.api import auth as auth_api
from app.api import chat as chat_api
from app.api import confirm as confirm_api
from app.api import fulfillment as fulfillment_api
from app.api import handoff as handoff_api
from app.api import orders as orders_api
from app.api import refunds as refunds_api
from app.api import refunds_ext as refunds_ext_api
from app.api import tickets as tickets_api
from app.db import SessionLocal, assert_schema_at_head, engine, utc_now
from app.errors import register_error_handlers
from app.memory.decay import router as memory_maintenance_router
from app.models import Base, ChatSession


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时幂等建表并拉起异步作业 worker;退出时优雅停 worker,再 dispose。

    W10/ISS-49:生产必须走异步队列(``enqueue`` 返回作业 id、worker 线程消费),
    不能在 ``lifespan`` 里漏掉 ``start_worker`` —— 否则队列退化为"在调用方
    事务里同步执行",P95 直接受业务写锁拖累。
    """
    Base.metadata.create_all(bind=engine)
    # R15(2026-11-21 评审核验新增):create_all 只建缺失的表、**从不 ALTER 已存在的表**,
    # 所以它兜不住 schema 漂移(dev.db 缺 order.delivered_at 却"看起来能起"就是这条)。
    # 这里校验库版本是否等于迁移链 head:无 alembic_version(create_all 路径)放行+告警,
    # 有但不等于 head 则**拒绝启动**。
    assert_schema_at_head(engine)
    jobs.start_worker(engine)
    sla_scheduler = sla.start_scheduler(engine)
    # W12-1:/metrics 的后台 5s 刷新线程(daemon,进程退出即止)。请求路径只读
    # 进程内缓存文本,不在这里做全表聚合;开关关闭或后台刷新显式禁用时不启动。
    if observability.metrics_enabled() and observability.background_refresh_enabled():
        observability.start_background_refresh()
    try:
        yield
    finally:
        if sla_scheduler is not None:
            sla_scheduler.stop()
        jobs.stop_worker(engine)
        engine.dispose()


logger = logging.getLogger("app.main")

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
        retry_after = security.check_rate_limit(identity, path=path)
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
    """**liveness**:静态 200,证明进程活着。

    R7(2026-11-21 企业工程评审):**刻意不探下游**。
    reference 明确「liveness 探 DB/Redis/第三方 = 级联故障放大器」——
    DB 抖动会导致全量实例被重启,把局部故障放大成全站故障。
    依赖就绪检查下沉到 ``/healthz/ready``。
    """
    return {"ok": True, "status": "healthy"}


def _db_reachable() -> bool:
    """readiness 用的最小依赖探针:``SELECT 1``。异常只记日志,不外泄细节。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - 探针不得因异常自身崩掉
        logger.warning("readiness 探针:数据库不可达(详情仅日志)", exc_info=True)
        return False


@app.get("/healthz/ready")
def healthz_ready() -> Response:
    """**readiness**:探 DB,不健康返 **503**(探针只看状态码,不能返 200 带 flag)。

    ⚠️ **单机形态取舍**:compose 只有一个 backend 实例,**摘流 = 全站下线**。
    所以本端点在单机下**只用于告警与人工判断,不用于摘流**;
    nginx 侧刻意不配基于它的主动摘流。多副本部署后才谈摘流,
    且届时要配最小可用实例数,否则 DB 抖动会从"部分请求失败"变成"全站不可用"。
    """
    try:
        ready = _db_reachable()
    except Exception:  # noqa: BLE001 - 探针**绝不能自己 500**,那会被误判成进程故障
        logger.warning("readiness 探针自身异常(详情仅日志)", exc_info=True)
        ready = False
    if ready:
        return JSONResponse(status_code=200, content={"ok": True, "status": "ready"})
    return JSONResponse(status_code=503, content={"ok": False, "status": "not_ready"})


app.include_router(orders_api.router)
app.include_router(tickets_api.router)
app.include_router(refunds_api.router)
app.include_router(refunds_ext_api.router)
app.include_router(fulfillment_api.router)
app.include_router(auth_api.router)
app.include_router(chat_api.router)
app.include_router(confirm_api.router)
app.include_router(handoff_api.router)
app.include_router(agent_api.router)
app.include_router(badcases_api.router)
app.include_router(kb_api.router)
app.include_router(memory_maintenance_router)


# ---------------------------------------------------------------------------
# W12-1:/metrics(Prometheus 文本格式,免鉴权但默认仅本机可读)
# ---------------------------------------------------------------------------
#
# 采集口径见 ``app.observability`` 模块 docstring:全部来自已落库的
# ``tool_events`` / ``token_usage`` / ``job_queue`` 与进程内缓存统计,后台每 5s
# 刷新一次文本缓存;请求路径只读缓存,缓存尚未建立时用本次请求的 session 现算一次。
# 暴露策略:``METRICS_ENABLED=0`` 关闭整个端点;默认只允许本机(含 TestClient),
# ``METRICS_ALLOW_REMOTE=1`` 才允许外部抓取(生产应交给 nginx 内网限定)。

#: Prometheus 文本格式的 Content-Type(0.0.4 版本,Prometheus 抓取协议要求)。
_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request, db: Session = Depends(get_db)) -> Response:
    """Prometheus 指标抓取端点(文本格式,零第三方依赖)。"""
    if not observability.metrics_enabled():
        return Response(content="metrics disabled\n", status_code=404, media_type="text/plain")
    client_host = request.client.host if request.client else None
    if not observability.allow_remote_clients() and not observability.client_is_local(client_host):
        return Response(
            content="metrics only available from localhost\n",
            status_code=403,
            media_type="text/plain",
        )
    text = observability.CACHE.get()
    if text is None or observability.CACHE.is_stale(observability.REFRESH_INTERVAL_SECONDS):
        text = observability.refresh(db)
    return Response(content=text, media_type=_PROMETHEUS_CONTENT_TYPE)


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


@contextmanager
def _ws_db_session() -> Iterator["Session"]:
    """取当前生效的 DB 会话。

    WS 端点不走 FastAPI 依赖注入,这里显式复用 ``app.dependency_overrides``
    里对 ``get_db`` 的覆盖(测试把库重定向到临时 SQLite),未覆盖时用默认
    ``SessionLocal``。
    """
    override = app.dependency_overrides.get(get_db)
    if override is None:
        with SessionLocal() as session:
            yield session
        return
    produced = override()
    if hasattr(produced, "__next__"):
        generator = produced
        try:
            yield next(generator)
        finally:
            generator.close()
    else:
        yield produced


def _buyer_owns_session(session_id: str, identity: security.Identity) -> bool:
    """W13 ISS-48:买家只能连**自己**的会话。

    旧实现只要带合法 buyer token 就能连 ``/ws/buyer/{任意 session_id}``,
    从而窃听他人会话的坐席回复。现在先查 ``ChatSession``,要求会话存在且
    ``customer_id`` 与 token 主体一致,否则 WS 端点以 4403 关闭。
    """
    if identity.is_internal:
        return True
    try:
        with _ws_db_session() as session:
            row = session.get(ChatSession, session_id)
            return row is not None and row.customer_id == identity.customer_id
    except Exception:  # pragma: no cover - DB 异常时按"不授权"处理
        return False


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
        if not _buyer_owns_session(session_id, identity):
            # 会话不存在或不属于该买家 → 拒绝订阅(防跨租户窃听)。
            await websocket.close(code=4403)
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
