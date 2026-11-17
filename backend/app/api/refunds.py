"""退款 API:创建申请(幂等)/审核/驳回/退款完成。

全部委托 services.refunds;创建申请带 idempotency_key 幂等,
重复提交命中返回已有单(idempotent_hit=true);金额超实付 422;活跃退款冲突 409。

W13 ISS-60:该组路由原先无身份校验与租户过滤,现统一挂 ``current_identity``
并按 ``identity.merchant_id`` 隔离(internal 模式不过滤)。

A1(2026-11-16,见 docs/审计发现_20261116.md):ISS-60 只补了**认证**,本组仍是
``ROLE_ANY`` 且只比租户,导致**买家 token 可以自己审核、自己打款完成**,甚至给同店
其他买家的订单退全款——直接击穿 PRD §4「退款永远只是创建申请单 + 人工审核,
永不自动打款」。现:
- ``approve`` / ``reject`` / ``refund`` 收归 agent(``require_agent``);
- ``create`` / ``get`` 仍允许买家,但补 **customer 归属校验**(只能动自己的单);
- ``decided_by`` 一律从 token 取,请求体值无身份效力(与 merchant_id 同一约定)。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api import assert_customer_scope, get_db, require_agent
from app.db import utc_now
from app.errors import NotFoundError, StateConflictError
from app.models import Order, RefundRequest
from app.schemas import Receipt, RefundCreate, RefundDecision, RefundOut
from app.security import Identity, current_identity
from app.services import refunds as refund_svc
from app.tools.refund import REFUNDABLE_STATUSES

router = APIRouter(tags=["refunds"])


def _assert_tenant(identity: Identity, refund: RefundRequest) -> None:
    """跨租户访问按"不存在"处理(不泄露他店退款单是否存在)。"""
    if identity.is_internal:
        return
    if refund.merchant_id != identity.merchant_id:
        raise NotFoundError(f"退款单不存在: {refund.id}")


def _assert_order_scope(db: Session, identity: Identity, order_id: str) -> Order:
    """A1:买家只能对**自己**的订单发起/查看退款;他人订单一律按"不存在"处理。

    返回订单对象供调用方复用(A2 的状态门要用),避免重复查库。
    internal 模式下仍取订单(A2 是业务政策,与鉴权模式无关),但不做归属校验。
    """
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    if identity.is_internal:
        return order
    if order.merchant_id != identity.merchant_id:
        raise NotFoundError(f"订单不存在: {order_id}")
    if not assert_customer_scope(identity, order.customer_id):
        raise NotFoundError(f"订单不存在: {order_id}")
    return order


def _assert_refundable(order: Order) -> None:
    """A2(2026-11-16):订单必须处于可退状态,否则从源头拒掉、不留脏单。

    缺陷:建单侧原先不看 ``order.status``,而完成侧 ``services.orders.refund_complete``
    守卫要求 ``delivered``。于是 ``shipped`` 订单退全款时,最后一笔把残额退到 0
    会 409 并 ``rollback``,**连退款单自身置「退款」都被回滚** → 永久卡在审核态。
    (部分退款反而能成,只有退到残额 0 才炸,行为不一致更难排查。)

    门放在**入口**而非服务层:服务层是机制(金额折算/幂等),入口才是政策落点;
    工具层 ``tools/refund.py`` 已有同一道门,这里复用它的常量保持单一事实源,
    同时不影响只测金额数学的服务层单测。
    """
    if order.status not in REFUNDABLE_STATUSES:
        raise StateConflictError(
            f"订单状态 {order.status} 不支持退款(仅已签收订单可退)", order.status
        )


def _decided_by(identity: Identity, payload: RefundDecision) -> str:
    """审核人一律取 token 主体;请求体的 ``decided_by`` 无身份效力(A1)。

    取 ``service_id``(坐席业务 id)而非 ``sub``(登录账号 id):审核留痕要能对上
    工作台里的坐席,与 ``api/tickets._service_id`` 同一口径。
    internal 模式(回归脚本)保留请求体值,与 ``merchant_id`` 的既有约定一致。
    """
    if identity.is_internal:
        return payload.decided_by or identity.sub
    return identity.service_id or identity.sub


@router.post(
    "/refunds", response_model=Receipt, status_code=status.HTTP_201_CREATED
)
def create_refund(
    payload: RefundCreate,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> Receipt:
    merchant_id = payload.merchant_id
    if not identity.is_internal:
        # 非 internal 模式:租户只能来自 token,请求体里的 merchant_id 不可信。
        merchant_id = identity.merchant_id
    _order = _assert_order_scope(db, identity, payload.order_id)
    _assert_refundable(_order)
    refund, idempotent_hit = refund_svc.create_refund_request(
        db,
        merchant_id=merchant_id,
        order_id=payload.order_id,
        amount_cents=payload.amount_cents,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        items=payload.items,
    )
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
        idempotent_hit=idempotent_hit,
    )


@router.get("/refunds/{refund_id}", response_model=RefundOut)
def get_refund(
    refund_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> RefundRequest:
    refund = db.get(RefundRequest, refund_id)
    if refund is None:
        raise NotFoundError(f"退款单不存在: {refund_id}")
    _assert_tenant(identity, refund)
    # A1:买家只能看自己的退款单(前端 stepper 走这个接口);他人单按不存在处理。
    if not identity.is_internal and identity.role == "buyer":
        order = db.get(Order, refund.order_id)
        if not assert_customer_scope(identity, order.customer_id if order else None):
            raise NotFoundError(f"退款单不存在: {refund_id}")
    return refund


@router.post("/refunds/{refund_id}/approve", response_model=Receipt)
def approve(
    refund_id: str,
    payload: RefundDecision,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_agent),
) -> Receipt:
    refund = db.get(RefundRequest, refund_id)
    if refund is None:
        raise NotFoundError(f"退款单不存在: {refund_id}")
    _assert_tenant(identity, refund)
    refund = refund_svc.approve(db, refund_id, _decided_by(identity, payload))
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/refunds/{refund_id}/reject", response_model=Receipt)
def reject(
    refund_id: str,
    payload: RefundDecision,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_agent),
) -> Receipt:
    refund = db.get(RefundRequest, refund_id)
    if refund is None:
        raise NotFoundError(f"退款单不存在: {refund_id}")
    _assert_tenant(identity, refund)
    refund = refund_svc.reject(db, refund_id, _decided_by(identity, payload))
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/refunds/{refund_id}/refund", response_model=Receipt)
def mark_refunded(
    refund_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_agent),
) -> Receipt:
    refund = db.get(RefundRequest, refund_id)
    if refund is None:
        raise NotFoundError(f"退款单不存在: {refund_id}")
    _assert_tenant(identity, refund)
    refund = refund_svc.mark_refunded(db, refund_id)
    return Receipt(
        refund_request_id=refund.id,
        status=refund.status,
        occurred_at_utc=utc_now(),
    )
