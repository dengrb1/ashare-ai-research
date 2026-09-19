"""
决策模块与现有 Builtin Pipeline 的集成层。

提供将现有 orchestration.builtin 流程适配到新决策协议的工具函数。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from ashare_ai.agents.decision.legacy import LegacyDecisionProvider
from ashare_ai.agents.decision.market_state import build_market_state_from_bundle
from ashare_ai.agents.decision.models import UnifiedDecision
from ashare_ai.agents.decision.router import DecisionRouter, create_decision_router

if TYPE_CHECKING:
    from ashare_ai.core.contracts import CompositeScore
    from ashare_ai.orchestration.bundle import CanonicalDailyBundle

logger = logging.getLogger(__name__)


async def generate_decision_from_composite_score(
    composite_score: CompositeScore,
    bundle: CanonicalDailyBundle,
    *,
    mode: str = "legacy",
    model_version: str = "legacy-v1.0.0",
) -> UnifiedDecision:
    """
    从现有 CompositeScore 生成 UnifiedDecision。

    集成点：在 builtin pipeline 计算出 CompositeScore 后调用此函数。

    Args:
        composite_score: 现有评分结果
        bundle: 市场数据 Bundle
        mode: 决策模式（"legacy" 或 "jev"）
        model_version: 模型版本

    Returns:
        UnifiedDecision: 统一决策结果
    """
    # 从 Bundle 构建 MarketState
    market_state = build_market_state_from_bundle(
        bundle=bundle,
        symbol=composite_score.symbol,
        trading_date=composite_score.trading_date,
    )

    if mode == "legacy":
        provider = LegacyDecisionProvider(model_version=model_version)
        return await provider.predict(market_state, composite_score=composite_score)
    else:
        # Jev 模式需要先训练模型
        logger.error(f"Unsupported decision mode: {mode}")
        raise ValueError(f"Decision mode '{mode}' not yet implemented")


async def generate_decision_for_symbol(
    bundle: CanonicalDailyBundle,
    symbol: str,
    trading_date: date,
    *,
    mode: str = "legacy",
    fallback_enabled: bool = False,
) -> UnifiedDecision:
    """
    为指定股票生成决策（不依赖预计算的 CompositeScore）。

    使用场景：新的端到端决策流程，绕过旧的 scoring pipeline。

    Args:
        bundle: 市场数据 Bundle
        symbol: 股票代码
        trading_date: 交易日期
        mode: 决策模式
        fallback_enabled: 是否启用回退

    Returns:
        UnifiedDecision: 统一决策结果
    """
    market_state = build_market_state_from_bundle(
        bundle=bundle,
        symbol=symbol,
        trading_date=trading_date,
    )

    router = create_decision_router(mode=mode, fallback_enabled=fallback_enabled)
    return await router.predict(market_state)


async def batch_generate_decisions(
    bundle: CanonicalDailyBundle,
    symbols: list[str],
    *,
    mode: str = "legacy",
    fallback_enabled: bool = False,
) -> dict[str, UnifiedDecision]:
    """
    批量生成决策。

    Args:
        bundle: 市场数据 Bundle
        symbols: 股票代码列表
        mode: 决策模式
        fallback_enabled: 是否启用回退

    Returns:
        dict[str, UnifiedDecision]: 股票代码到决策的映射
    """
    router = create_decision_router(mode=mode, fallback_enabled=fallback_enabled)
    results: dict[str, UnifiedDecision] = {}

    for symbol in symbols:
        try:
            market_state = build_market_state_from_bundle(
                bundle=bundle,
                symbol=symbol,
                trading_date=bundle.trading_date,
            )
            decision = await router.predict(market_state)
            results[symbol] = decision
        except Exception as e:
            logger.error(f"Failed to generate decision for {symbol}: {e}")
            if not fallback_enabled:
                raise

    return results
