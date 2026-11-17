"""API 路由包。get_db 依赖函数放这里,避免 main ↔ api 循环导入。"""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app import security
from app.db import SessionLocal
from app.errors import AppError


def get_db() -> Iterator[Session]:
    """FastAPI 依赖:每请求一个 Session,请求结束关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_agent(request: Request) -> security.Identity:
    """坐席专属写操作的授权依赖(A1 修复,2026-11-16)。

    背景:W13-ISS-60 把**认证**扩到全部路由(``required_role`` 白名单式,非公开
    路径一律要 Bearer),但 ``/orders`` ``/tickets`` ``/refunds`` 仍是 ``ROLE_ANY``,
    且这三组 router 内部只比 ``merchant_id``。结果一个买家 token 就能推他人订单签收、
    自审自退全款、冒充坐席领单关单——击穿 PRD §4「退款只做申请单 + 人工审核」。
    本依赖把**授权**补齐:认证归中间件,角色归这里。

    ``AUTH_MODE=internal`` 放行,与同层 ``_assert_tenant`` 的 internal 约定一致
    (回归脚本 check_w1_cases.py / demo_w2.py 不带 token);对外服务默认 enforce,
    非 agent 一律 403,错误体与 W8 中间件同形。

    注意:这与 ``api/kb.py`` 里同名依赖的区别是——kb 那份在 internal 下会退回
    自行解析 Bearer(它要保住"internal 也按 token 判角色"的既有行为),
    本份在 internal 下直接放行,以免打断数百条不带 token 的存量回归。
    """
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise AppError(
            code="AUTH_REQUIRED",
            message="缺少身份信息(请先登录)",
            status_code=401,
        )
    if identity.is_internal:
        return identity
    if identity.role != "agent":
        raise AppError(
            code="FORBIDDEN",
            message=f"该接口需要 agent 角色,当前角色为 {identity.role}",
            status_code=403,
        )
    return identity


def assert_customer_scope(identity: security.Identity, customer_id: str | None) -> bool:
    """买家是否有权访问该 customer 的资源(internal 与 agent 不受此限)。

    返回 False 时调用方应按"不存在"处理(404),不泄露他人资源存在性
    ——与 ``_assert_tenant`` 的跨租户口径一致。
    """
    if identity.is_internal or identity.role != "buyer":
        return True
    return bool(customer_id) and customer_id == identity.customer_id
