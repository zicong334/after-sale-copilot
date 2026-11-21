"""语义缓存的 Redis L2 层 + 失效广播(R8,2026-11-21 企业工程评审)。

为什么需要
----------
原先只有**进程内 L1**(512 条 LRU、TTL 30min)。多实例时各自一份,
一致性窗口 = TTL —— 政策改了各实例不同时生效。

设计取舍(**刻意不把 TF-IDF 矩阵搬进 Redis**)
--------------------------------------------
- **L1 仍负责语义相似匹配**(改述命中);
- **L2 只做归一化精确键命中**。把相似度矩阵与候选语料塞进 Redis 既贵又难维护,
  而 L2 的价值主要是"跨实例复用已算过的答案",精确键足够。
  ⚠️ **L2 不命中改述**是有意取舍,不是缺陷。
- 失效走**按租户分频道的 Pub/Sub**,消息体**只带 key**不带正文
  (带正文等于又开一个内容出口)。

⚠️ 验证边界
-----------
本机无 redis-server。已验的只有本模块自写逻辑(键/频道命名、TTL、故障降级、L1↔L2 交互)。
**未运行时验证**:跨实例命中率、Pub/Sub 真实失效延迟、L1/L2 版本差。
清标记需 `deploy/verify_runtime.sh` 在真 Redis 上跑过。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

REDIS_URL_ENV = "REDIS_URL"
_KEY_PREFIX = "sc:l2"
_CHANNEL_PREFIX = "sc:invalidate"


def _digest(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:32]


class RedisL2Cache:
    """精确键 L2。``client`` 可注入(测试用假客户端验本模块逻辑)。"""

    def __init__(self, *, client: Any, ttl_seconds: int = 1800) -> None:
        self._client = client
        self._ttl = int(ttl_seconds)

    def key_for(self, merchant_id: str, normalized: str) -> str:
        """租户隔离 + 归一化查询串摘要。**跨租户键必须不同**,否则串读。"""
        return f"{_KEY_PREFIX}:{merchant_id or '-'}:{_digest(normalized)}"

    def channel_for(self, merchant_id: str) -> str:
        return f"{_CHANNEL_PREFIX}:{merchant_id or '-'}"

    def get(self, merchant_id: str, normalized: str):
        """命中返回 CacheEntry,未命中/故障返回 None(**绝不抛**)。"""
        from app.cache import CacheEntry

        try:
            raw = self._client.get(self.key_for(merchant_id, normalized))
        except Exception:  # noqa: BLE001 - 缓存是辅助路径
            logger.warning("L2 读失败(降级为未命中)", exc_info=True)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return CacheEntry(
                reply=str(data.get("reply") or ""),
                citations=list(data.get("citations") or []),
                intent=str(data.get("intent") or ""),
                confidence=float(data.get("confidence") or 0.0),
                intent_reason=str(data.get("intent_reason") or ""),
                # **必须还原 created_at**:CacheEntry 默认 0.0,不还原会让回填 L1 的条目
                # 被 _purge_expired 立刻判过期(自测抓到过);而且 TTL 必须锚在**原始入库时刻**,
                # 不能每次 L2 读都重置,否则热门键永不过期。
                created_at=float(data.get("created_at") or 0.0),
            )
        except Exception:  # noqa: BLE001 - 脏数据当未命中,不带崩主链路
            logger.warning("L2 条目反序列化失败(当未命中)", exc_info=True)
            return None

    def set(self, merchant_id: str, normalized: str, entry: Any) -> None:
        """写 L2,**必须带 TTL**(否则又是无界增长)。故障只告警。"""
        payload = json.dumps(
            {
                "reply": entry.reply,
                "citations": list(entry.citations or []),
                "intent": entry.intent,
                "confidence": entry.confidence,
                "intent_reason": entry.intent_reason,
                "created_at": float(getattr(entry, "created_at", 0.0) or 0.0),
            },
            ensure_ascii=False,
        )
        try:
            self._client.set(self.key_for(merchant_id, normalized), payload, ex=self._ttl)
        except Exception:  # noqa: BLE001
            logger.warning("L2 写失败(忽略)", exc_info=True)

    def invalidate(self, merchant_id: str, normalized: str | None = None) -> None:
        """删本键(或整租户由调用方遍历)+ 广播失效。**消息只带 key,不带正文**。"""
        try:
            if normalized is not None:
                key = self.key_for(merchant_id, normalized)
                self._client.delete(key)
                self._client.publish(self.channel_for(merchant_id), key)
            else:
                # 整租户失效:只广播频道级信号,由订阅方清自己的 L1。
                # 刻意不用 KEYS/SCAN 遍历删除(参考判据:禁 KEYS *,且遍历删除 costly)。
                self._client.publish(self.channel_for(merchant_id), "*")
        except Exception:  # noqa: BLE001
            logger.warning("L2 失效广播失败(忽略)", exc_info=True)


def _default_client_factory(url: str) -> Any:
    import redis  # type: ignore[import-not-found]

    return redis.Redis.from_url(url, decode_responses=True)


def build_l2(client_factory: Callable[[str], Any] | None = None):
    """按 ``REDIS_URL`` 决定是否启用 L2。

    未设 → None(**纯 L1,存量路径一字不变**);
    设了但客户端建不起来 → None + 告警(**不阻断启动**)。
    """
    url = (os.environ.get(REDIS_URL_ENV) or "").strip()
    if not url:
        return None
    factory = client_factory or _default_client_factory
    try:
        return RedisL2Cache(client=factory(url))
    except Exception:  # noqa: BLE001
        logger.warning("L2 缓存后端不可用,退回纯 L1(跨实例一致性窗口=TTL)", exc_info=True)
        return None