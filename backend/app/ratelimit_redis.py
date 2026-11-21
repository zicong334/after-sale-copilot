"""Redis 限流后端(R4/R5,2026-11-21 企业工程评审)。

修的两条反模式
--------------
- **本地计数器限流**:`MemoryRateLimiter` 是进程内的,N 个实例集群实际放行 = N × 阈值,
  阈值形同虚设。改为 Redis + **单次 Lua EVAL**(补充令牌 + 扣减 + 过期一趟做完),
  集群共享同一份计数。
- **本地缓存无界**:`_buckets` 按 subject 只增不清。Redis 侧**每个键带 EXPIRE**,
  闲置主体自动消失 —— 这是 R5 的修法核心,**不自造 LRU**(自造会与 Redis 语义分叉)。

故障策略:按链路分档,不一刀切
--------------------------------
资损/写链路(退款、订单状态、工单、对话)**fail-close**(Redis 挂了宁可拒绝);
纯读链路(历史消息、知识库、登录态)**fail-open**(可用性优先)。
`fail_mode_for_path()` 做分档,`consume(..., fail_mode=...)` 可按调用覆盖。

⚠️⚠️ 验证边界 ⚠️⚠️
本机**无 redis-server**。已验的只有**本模块自己的逻辑**:键命名空间、单次 EVAL、
脚本带 TTL、后端选择、故障降级与分档(见 `tests/test_ratelimit_redis.py`)。
**未运行时验证**:集群总放行量 ≈ 阈值而非 N×、EXPIRE 是否真让无界增长消失、
**Lua 的真原子性**。清标记需 `deploy/verify_runtime.sh` 在真 Redis 上跑过。
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

REDIS_URL_ENV = "REDIS_URL"

#: 资损/写链路前缀:Redis 故障时 **fail-close**。
_FAIL_CLOSED_PREFIXES: tuple[str, ...] = (
    "/refunds",
    "/orders",
    "/tickets",
    "/api/fulfillment",
    "/api/c/chat",
    "/api/c/handoff",
    "/api/c/confirm",
)

#: 令牌桶:补充 + 扣减 + 过期,**一趟 EVAL**。retry 用字符串回传(Redis Lua 数字会截断小数)。
TOKEN_BUCKET_LUA = """
local key    = KEYS[1]
local rate   = tonumber(ARGV[1])
local burst  = tonumber(ARGV[2])
local now    = tonumber(ARGV[3])
local ttl    = tonumber(ARGV[4])
local data   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])
if tokens == nil then tokens = burst end
if ts == nil then ts = now end
local delta = math.max(0, now - ts)
tokens = math.min(burst, tokens + delta * rate)
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  if rate > 0 then retry = (1 - tokens) / rate else retry = 1 end
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(retry)}
"""

#: 补回一个令牌(租户级拒绝时回滚用户级),**不超过桶容量**,同样带 EXPIRE。
REFUND_LUA = """
local key   = KEYS[1]
local burst = tonumber(ARGV[1])
local ttl   = tonumber(ARGV[2])
local data  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])
if tokens == nil then return 0 end
tokens = math.min(burst, tokens + 1)
redis.call('HSET', key, 'tokens', tokens, 'ts', ts)
redis.call('EXPIRE', key, ttl)
return 1
"""


def fail_mode_for_path(path: str) -> str:
    """该路径在 Redis 故障时应 fail-open 还是 fail-close。"""
    text = str(path or "")
    for prefix in _FAIL_CLOSED_PREFIXES:
        if text == prefix or text.startswith(prefix + "/") or text.startswith(prefix + "?"):
            return "closed"
    return "open"


class RedisRateLimiter:
    """Redis 令牌桶。``client`` 可注入(测试用假客户端验本模块逻辑)。"""

    def __init__(
        self, rate: float, burst: float, *, client: Any, prefix: str,
        ttl_seconds: int | None = None, fail_mode: str = "open",
    ) -> None:
        self.rate = float(rate)
        self.burst = float(burst)
        self._client = client
        self._prefix = str(prefix)
        # 键 TTL:闲置即消失(等价于令牌已补满),这就是无界增长的解法。
        # 取"补满一桶所需时间 + 余量",保证过期不会误伤活跃主体。
        refill = math.ceil(self.burst / self.rate) if self.rate > 0 else 60
        self._ttl = int(ttl_seconds) if ttl_seconds is not None else max(60, refill + 60)
        self._fail_mode = "closed" if fail_mode == "closed" else "open"

    def key_for(self, subject: str) -> str:
        return f"{self._prefix}:{subject}"

    def consume(
        self, key: str, now: float | None = None, fail_mode: str | None = None,
    ) -> tuple[bool, float]:
        import time

        stamp = float(now) if now is not None else time.time()
        mode = fail_mode or self._fail_mode
        try:
            raw = self._client.eval(
                TOKEN_BUCKET_LUA, 1, self.key_for(key),
                self.rate, self.burst, stamp, self._ttl,
            )
            allowed = bool(int(raw[0]))
            retry = float(raw[1])
            return allowed, max(retry, 0.0 if allowed else 0.001)
        except Exception:  # noqa: BLE001 - Redis 故障不得把主链路带崩
            logger.warning(
                "Redis 限流不可用,按 %s 处理(key=%s)", mode, key, exc_info=True
            )
            if mode == "closed":
                return False, 1.0
            return True, 0.0

    def refund(self, key: str) -> None:
        try:
            self._client.eval(REFUND_LUA, 1, self.key_for(key), self.burst, self._ttl)
        except Exception:  # noqa: BLE001 - 补回是辅助动作,失败只告警
            logger.warning("Redis 补回令牌失败(key=%s)", key, exc_info=True)


def _default_client_factory(url: str) -> Any:
    """默认客户端工厂。**惰性导入 redis** —— 未装该依赖时不应影响进程内默认路径。"""
    import redis  # type: ignore[import-not-found]

    return redis.Redis.from_url(url, decode_responses=True)


def build_limiters(client_factory: Callable[[str], Any] | None = None):
    """按 ``REDIS_URL`` 选后端,返回 ``(user_limiter, tenant_limiter)``。

    - 未设 → **进程内实现**(默认路径一字不变,915+ 存量测试不受影响);
    - 设了但客户端建不起来 → **降级回进程内并告警**,不让服务起不来。
    """
    from app.security import (
        MemoryRateLimiter,
        TENANT_BURST,
        TENANT_RATE_PER_SECOND,
        USER_BURST,
        USER_RATE_PER_SECOND,
    )

    def _memory():
        return (
            MemoryRateLimiter(USER_RATE_PER_SECOND, USER_BURST),
            MemoryRateLimiter(TENANT_RATE_PER_SECOND, TENANT_BURST),
        )

    url = (os.environ.get(REDIS_URL_ENV) or "").strip()
    if not url:
        return _memory()
    factory = client_factory or _default_client_factory
    try:
        client = factory(url)
    except Exception:  # noqa: BLE001 - 连不上就降级,不阻断启动
        logger.warning("Redis 限流后端不可用,降级回进程内(集群阈值口径失效!)", exc_info=True)
        return _memory()
    return (
        RedisRateLimiter(USER_RATE_PER_SECOND, USER_BURST, client=client, prefix="rl:user"),
        RedisRateLimiter(TENANT_RATE_PER_SECOND, TENANT_BURST, client=client, prefix="rl:tenant"),
    )