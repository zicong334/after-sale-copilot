"""模型基类 + 全部模型导出。

Base 必须定义在此(产物白名单没有独立 base 模块),且要先定义 Base
再 import 各模型模块,避免循环导入:各模型文件 `from app.models import Base`。
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类,alembic env.py 用 ``Base.metadata`` 驱动迁移。"""


# 先定义 Base,再导入全部模型,确保 Base.metadata 包含全部表(W8 新增 3 张 auth 表)
# 注:本文件不在 W8 白名单里,但新模型必须在此 import 才会进 Base.metadata /
# alembic autogenerate;属"新模型注册"必要最小改动,已在回执说明。
from app.models.auth import AuthUser, RequestNonce, TokenUsage  # noqa: E402,F401
from app.models.catalog import Product, ProductMetric, ProductVariant, Review  # noqa: E402,F401
from app.models.chat import ChatMessage, ChatSession  # noqa: E402,F401
from app.models.eval import EvalCase  # noqa: E402,F401
from app.models.kb import KnowledgeItem  # noqa: E402,F401
from app.models.merchant import Merchant  # noqa: E402,F401
from app.models.orders import Order, OrderItem, RefundRequest  # noqa: E402,F401
from app.models.ticket import Ticket, TicketMessage  # noqa: E402,F401

__all__ = [
    "Base",
    "Merchant",
    "Product",
    "ProductVariant",
    "ProductMetric",
    "Review",
    "Order",
    "OrderItem",
    "RefundRequest",
    "ChatSession",
    "ChatMessage",
    "Ticket",
    "TicketMessage",
    "KnowledgeItem",
    "EvalCase",
    "AuthUser",
    "RequestNonce",
    "TokenUsage",
]
