"""订单 API:GET 列表/单个、POST 创建、POST 状态推进(pay/ship/deliver)。

状态推进全部委托 services.orders 的守卫式实现;非法迁移返回 409 STATE_CONFLICT。
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
from app.services import orders as order_svc

router = APIRouter(tags=["orders"])


@router.post("/orders", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(payload: OrderCreate, db: Session = Depends(get_db)) -> Order:
    order = Order(
        merchant_id=payload.merchant_id,
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
def list_orders(db: Session = Depends(get_db)) -> list[Order]:
    return list(
        db.execute(select(Order).order_by(Order.created_at.desc())).scalars()
    )


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: str, db: Session = Depends(get_db)) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    return order


def _receipt(order: Order) -> Receipt:
    return Receipt(
        order_id=order.id,
        status=order.status,
        occurred_at_utc=utc_now(),
    )


@router.post("/orders/{order_id}/pay", response_model=Receipt)
def pay(order_id: str, db: Session = Depends(get_db)) -> Receipt:
    return _receipt(order_svc.pay(db, order_id))


@router.post("/orders/{order_id}/ship", response_model=Receipt)
def ship(order_id: str, db: Session = Depends(get_db)) -> Receipt:
    return _receipt(order_svc.ship(db, order_id))


@router.post("/orders/{order_id}/deliver", response_model=Receipt)
def deliver(order_id: str, db: Session = Depends(get_db)) -> Receipt:
    return _receipt(order_svc.deliver(db, order_id))
