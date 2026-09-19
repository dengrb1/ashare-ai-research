"""
决策缓存层（Redis 实现）。

减少重复计算，加速 API 响应。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from ashare_ai.agents.decision.models import UnifiedDecision

logger = logging.getLogger(__name__)


class DecisionCache:
    """决策结果缓存"""

    def __init__(self, redis: Redis, ttl: int = 3600):
        """
        Args:
            redis: Redis 异步客户端
            ttl: 缓存过期时间（秒），默认 1 小时
        """
        self.redis = redis
        self.ttl = ttl
        self.key_prefix = "decision:cache:"

    def _make_key(
        self,
        symbol: str,
        trading_date: date,
        mode: str,
        model_version: str,
    ) -> str:
        """生成缓存键"""
        key_data = f"{symbol}:{trading_date}:{mode}:{model_version}"
        key_hash = hashlib.md5(key_data.encode()).hexdigest()[:12]
        return f"{self.key_prefix}{key_hash}"

    async def get(
        self,
        symbol: str,
        trading_date: date,
        mode: str,
        model_version: str,
    ) -> UnifiedDecision | None:
        """
        从缓存获取决策。

        Args:
            symbol: 股票代码
            trading_date: 交易日期
            mode: 决策模式
            model_version: 模型版本

        Returns:
            UnifiedDecision | None: 缓存的决策，不存在则返回 None
        """
        from ashare_ai.agents.decision.models import UnifiedDecision

        key = self._make_key(symbol, trading_date, mode, model_version)

        try:
            data = await self.redis.get(key)
            if data is None:
                return None

            decision = UnifiedDecision.model_validate_json(data)
            logger.debug(f"Cache hit: {symbol} on {trading_date}")
            return decision

        except Exception as e:
            logger.warning(f"Failed to get from cache: {e}")
            return None

    async def set(
        self,
        decision: UnifiedDecision,
        ttl: int | None = None,
    ) -> None:
        """
        将决策写入缓存。

        Args:
            decision: 决策结果
            ttl: 过期时间（秒），None 使用默认值
        """
        key = self._make_key(
            decision.symbol,
            decision.trading_date,
            decision.model_version.split("-")[0],  # "legacy-v1.0.0" -> "legacy"
            decision.model_version,
        )

        try:
            data = decision.model_dump_json()
            await self.redis.setex(key, ttl or self.ttl, data)
            logger.debug(f"Cached decision: {decision.symbol} on {decision.trading_date}")

        except Exception as e:
            logger.warning(f"Failed to write to cache: {e}")

    async def invalidate(
        self,
        symbol: str,
        trading_date: date,
        mode: str | None = None,
    ) -> int:
        """
        失效缓存。

        Args:
            symbol: 股票代码
            trading_date: 交易日期
            mode: 决策模式，None 则失效所有模式

        Returns:
            int: 失效的键数量
        """
        if mode is None:
            # 失效所有模式
            pattern = f"{self.key_prefix}{symbol}:{trading_date}:*"
        else:
            pattern = f"{self.key_prefix}{symbol}:{trading_date}:{mode}:*"

        try:
            keys = []
            async for key in self.redis.scan_iter(match=pattern, count=100):
                keys.append(key)

            if keys:
                deleted = await self.redis.delete(*keys)
                logger.info(f"Invalidated {deleted} cached decisions for {symbol}")
                return deleted

            return 0

        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")
            return 0

    async def clear_expired(self) -> None:
        """清理过期缓存（由定时任务调用）"""
        # Redis 自动处理过期，此方法预留给手动清理
        pass

    async def get_stats(self) -> dict[str, int]:
        """获取缓存统计"""
        try:
            keys = []
            async for key in self.redis.scan_iter(match=f"{self.key_prefix}*", count=1000):
                keys.append(key)

            return {
                "total_keys": len(keys),
                "prefix": self.key_prefix,
            }

        except Exception as e:
            logger.warning(f"Failed to get cache stats: {e}")
            return {"total_keys": 0, "prefix": self.key_prefix}


class CachedDecisionRouter:
    """
    带缓存的决策路由器。

    在 DecisionRouter 外层包装缓存逻辑。
    """

    def __init__(self, router: "DecisionRouter", cache: DecisionCache):
        """
        Args:
            router: 原始决策路由器
            cache: 决策缓存
        """
        self.router = router
        self.cache = cache

    async def predict(self, market_state: "MarketState") -> UnifiedDecision:
        """
        生成决策（带缓存）。

        Args:
            market_state: 市场状态

        Returns:
            UnifiedDecision: 决策结果（可能来自缓存）
        """
        # 尝试从缓存获取
        cached = await self.cache.get(
            symbol=market_state.symbol,
            trading_date=market_state.trading_date,
            mode=self.router.mode,
            model_version=self.router.current_provider.model_version,
        )

        if cached is not None:
            return cached

        # 缓存未命中，调用实际路由器
        decision = await self.router.predict(market_state)

        # 写入缓存
        await self.cache.set(decision)

        return decision

    async def predict_batch(
        self,
        market_states: list["MarketState"],
    ) -> list[UnifiedDecision]:
        """
        批量生成决策（带缓存）。

        Args:
            market_states: 市场状态列表

        Returns:
            list[UnifiedDecision]: 决策结果列表
        """
        import asyncio

        tasks = [self.predict(state) for state in market_states]
        return await asyncio.gather(*tasks)


async def create_cached_router(
    mode: str,
    redis_url: str,
    cache_ttl: int = 3600,
    fallback_enabled: bool = False,
) -> CachedDecisionRouter:
    """
    创建带缓存的决策路由器。

    Args:
        mode: 决策模式
        redis_url: Redis 连接 URL
        cache_ttl: 缓存过期时间（秒）
        fallback_enabled: 是否启用回退

    Returns:
        CachedDecisionRouter: 带缓存的路由器
    """
    from redis.asyncio import from_url

    from ashare_ai.agents.decision.router import create_decision_router

    redis = await from_url(redis_url, decode_responses=True)
    cache = DecisionCache(redis, ttl=cache_ttl)
    router = create_decision_router(mode=mode, fallback_enabled=fallback_enabled)

    return CachedDecisionRouter(router, cache)
