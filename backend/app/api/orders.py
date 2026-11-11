"""订单 API:GET 列表/单个、POST 创建、POST 状态推进(pay/ship/deliver)。

状态推进全部委托 services.orders 的守卫式实现;非法迁移返回 409 STATE_CONFLICT。

W13 ISS-60:该组路由原先没有任何身份校验与租户过滤(enforce 模式下裸奔,
任意登录者可跨店读写订单)。现统一挂 ``current_identity`` 并按
``identity.merchant_id`` 做租户隔离;``AUTH_MODE=internal``(回归脚本)下不过滤。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import get_db
from app.db import utc_now
from app.errors import NotFoundError
from app.models import Order
from app.schemas import OrderCreate, OrderOut, Receipt
from app.security import Identity, current_identity
from app.services import orders as order_svc

router = APIRouter(tags=["orders"])


def _assert_tenant(identity: Identity, order: Order) -> None:
    """跨租户访问按"不存在"处理(不泄露他店订单是否存在)。"""
    if identity.is_internal:
        return
    if order.merchant_id != identity.merchant_id:
        raise NotFoundError(f"订单不存在: {order.id}")


@router.post("/orders", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> Order:
    merchant_id = payload.merchant_id
    if not identity.is_internal:
        # 非 internal 模式:租户只能来自 token,请求体里的 merchant_id 不可信。
        merchant_id = identity.merchant_id
    order = Order(
        merchant_id=merchant_id,
        customer_id=payload.customer_id,
        status="created",
        total_cents=payload.total_cents,
        currency=payload.currency,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@router.get("/orders", response_model=list[OrderOut])
def list_orders(
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> list[Order]:
    """订单列表。

    W11 B1:除了商户租户隔离,还必须按 ``customer_id`` 过滤 —— 买家 token
    只能看到自己的订单(此前 ``/orders?customer_id=`` 仅作建议,能越权读到
    同店其他买家的订单)。
    """
    stmt = select(Order).order_by(Order.created_at.desc())
    if not identity.is_internal:
        stmt = stmt.where(Order.merchant_id == identity.merchant_id)
        if identity.role == "buyer" and identity.customer_id:
            stmt = stmt.where(Order.customer_id == identity.customer_id)
    return list(db.execute(stmt).scalars())


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(
    order_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    _assert_tenant(identity, order)
    return order


def _receipt(order: Order) -> Receipt:
    return Receipt(
        order_id=order.id,
        status=order.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/orders/{order_id}/pay", response_model=Receipt)
def pay(
    order_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> Receipt:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    _assert_tenant(identity, order)
    return _receipt(order_svc.pay(db, order_id))


@router.post("/orders/{order_id}/ship", response_model=Receipt)
def ship(
    order_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> Receipt:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    _assert_tenant(identity, order)
    return _receipt(order_svc.ship(db, order_id))


@router.post("/orders/{order_id}/deliver", response_model=Receipt)
def deliver(
    order_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(current_identity),
) -> Receipt:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    _assert_tenant(identity, order)
    return _receipt(order_svc.deliver(db, order_id))
