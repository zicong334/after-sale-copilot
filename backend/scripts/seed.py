"""seed 脚本:1 merchant、30 订单(覆盖 6 状态)、8 工单(覆盖 4 状态)+ 消息。

幂等策略:固定 ``seed-`` 前缀 id,先删子后删父,再重新插入;连跑两遍数据量不变。

- 客服/客户无独立表,只用字符串 id:客服 ``seed-agent-1/2``,客户 ``seed-cust-001..010``。
- 订单分布:created×6、paid×6、shipped×6、delivered×6、closed×3、refunded×3。
  - seed-order-019 = 9 天前 delivered(W1-C 判责用);seed-order-020 = 3 天前 delivered。
  - seed-order-012 = paid 未发货。
  - 注:Order 无 delivered_at/paid_at/shipped_at 字段(W1-A 冻结),「9 天前 delivered」用 created_at 近似。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, utc_now
from app.models import (
    AuthUser,
    Merchant,
    Order,
    OrderItem,
    RefundRequest,
    Ticket,
    TicketMessage,
)
from app.security import hash_password

MERCHANT_ID = "seed-merchant-1"
AGENT_1 = "seed-agent-1"
AGENT_2 = "seed-agent-2"

#: W8 演示账号(明文口令只写在 README「演示账号」节;库里只存 pbkdf2 哈希)。
#: buyer 绑定 seed-cust-001/002,agent 绑定 seed-agent-1/2。
DEMO_PASSWORD = "demo1234"
DEMO_ACCOUNTS: list[dict[str, str]] = [
    {
        "username": "buyer_demo",
        "role": "buyer",
        "customer_id": "seed-cust-001",
        "service_id": "",
        "display": "买家演示账号(客户 seed-cust-001)",
    },
    {
        "username": "buyer_demo2",
        "role": "buyer",
        "customer_id": "seed-cust-002",
        "service_id": "",
        "display": "买家演示账号 2(客户 seed-cust-002)",
    },
    {
        "username": "agent_demo",
        "role": "agent",
        "customer_id": "",
        "service_id": AGENT_1,
        "display": "坐席演示账号(客服 seed-agent-1)",
    },
    {
        "username": "agent_demo2",
        "role": "agent",
        "customer_id": "",
        "service_id": AGENT_2,
        "display": "坐席演示账号 2(客服 seed-agent-2)",
    },
]

# 30 单状态序列:6×created + 6×paid + 6×shipped + 6×delivered + 3×closed + 3×refunded
_ORDER_STATUSES: list[str] = (
    ["created"] * 6
    + ["paid"] * 6
    + ["shipped"] * 6
    + ["delivered"] * 6
    + ["closed"] * 3
    + ["refunded"] * 3
)


def _clear(session: Session) -> None:
    """先删子后删父(幂等)。seed 只碰 seed- 前缀数据。"""
    session.execute(delete(AuthUser).where(AuthUser.id.like("seed-user-%")))
    session.execute(
        delete(TicketMessage).where(TicketMessage.ticket_id.like("seed-ticket-%"))
    )
    session.execute(delete(Ticket).where(Ticket.id.like("seed-ticket-%")))
    session.execute(delete(RefundRequest).where(RefundRequest.merchant_id == MERCHANT_ID))
    session.execute(delete(OrderItem).where(OrderItem.order_id.like("seed-order-%")))
    session.execute(delete(Order).where(Order.merchant_id == MERCHANT_ID))
    session.execute(delete(Merchant).where(Merchant.id == MERCHANT_ID))
    session.flush()


def _seed_orders(session: Session, now) -> list[str]:
    """插入 30 单,返回 order_id 列表(供工单关联)。"""
    order_ids: list[str] = []
    for idx in range(30):
        order_id = f"seed-order-{idx + 1:03d}"
        customer_id = f"seed-cust-{idx % 10 + 1:03d}"
        # 特殊:019=9 天前 delivered,020=3 天前 delivered;其余按 idx 递减
        if idx == 18:  # seed-order-019
            created_at = now - timedelta(days=9)
        elif idx == 19:  # seed-order-020
            created_at = now - timedelta(days=3)
        else:
            created_at = now - timedelta(days=idx % 12)
        session.add(
            Order(
                id=order_id,
                merchant_id=MERCHANT_ID,
                customer_id=customer_id,
                status=_ORDER_STATUSES[idx],
                total_cents=10000 + idx * 100,
                currency="CNY",
                created_at=created_at,
            )
        )
        order_ids.append(order_id)
    return order_ids


def _seed_tickets(session: Session, order_ids: list[str], now) -> None:
    """插入 8 工单覆盖 4 状态 + 若干消息。"""
    # (status, claimed_by, order_idx, handoff_reason, 消息列表[(author, content)])
    specs = [
        ("queued", None, 6, None, []),  # seed-order-007 (paid)
        ("queued", None, 18, None, []),  # seed-order-019 (9 天前 delivered)
        ("claimed", AGENT_1, 18, None, [("agent", "您好,关于退款申请我来跟进")]),
        ("claimed", AGENT_2, 19, None, [("agent", "已受理,请稍候")]),  # 3 天前 delivered
        ("waiting_customer", AGENT_1, 11, "需客户补充物流凭证", [("user", "申请退款"), ("agent", "请提供物流单号")]),
        ("waiting_customer", AGENT_2, 12, "等待客户确认方案", [("agent", "我们提供两种方案可选")]),
        ("closed", AGENT_1, 24, None, [("user", "问题已解决"), ("system", "工单已关闭")]),
        ("closed", AGENT_2, 25, None, [("user", "满意"), ("system", "工单已关闭")]),
    ]
    for i, (status, claimed_by, order_idx, handoff_reason, messages) in enumerate(specs):
        ticket_id = f"seed-ticket-{i + 1:03d}"
        order = order_ids[order_idx]
        customer_id = f"seed-cust-{order_idx % 10 + 1:03d}"
        session.add(
            Ticket(
                id=ticket_id,
                merchant_id=MERCHANT_ID,
                customer_id=customer_id,
                order_id=order,
                status=status,
                claimed_by=claimed_by,
                handoff_reason=handoff_reason,
                last_msg_at=now if messages else None,
            )
        )
        for j, (author, content) in enumerate(messages):
            session.add(
                TicketMessage(
                    ticket_id=ticket_id,
                    author_type=author,
                    content=content,
                    is_internal=False,
                )
            )


def _seed_accounts(session: Session) -> None:
    """W8 演示账号:4 个固定 id 的 auth_user 行(口令统一 ``DEMO_PASSWORD``)。"""
    for account in DEMO_ACCOUNTS:
        session.add(
            AuthUser(
                id=f"seed-user-{account['username']}",
                username=account["username"],
                password_hash=hash_password(DEMO_PASSWORD),
                role=account["role"],
                merchant_id=MERCHANT_ID,
                customer_id=account["customer_id"] or None,
                service_id=account["service_id"] or None,
            )
        )


def seed(session: Session) -> dict[str, int]:
    """执行 seed,返回各表行数统计(供幂等验证比较)。"""
    _clear(session)
    now = utc_now()
    session.add(
        Merchant(
            id=MERCHANT_ID,
            name="seed 测试店铺",
            platform="demo",
            locale="zh-CN",
            currency="CNY",
            tz="UTC",
        )
    )
    order_ids = _seed_orders(session, now)
    _seed_tickets(session, order_ids, now)
    _seed_accounts(session)
    session.commit()

    return {
        "merchant": session.scalar(select(func.count()).select_from(Merchant)),
        "order": session.scalar(select(func.count()).select_from(Order)),
        "ticket": session.scalar(select(func.count()).select_from(Ticket)),
        "ticket_message": session.scalar(
            select(func.count()).select_from(TicketMessage)
        ),
        "auth_user": session.scalar(select(func.count()).select_from(AuthUser)),
    }


def main() -> None:
    with SessionLocal() as session:
        counts = seed(session)
    print("seed 完成:", counts)


if __name__ == "__main__":
    main()
