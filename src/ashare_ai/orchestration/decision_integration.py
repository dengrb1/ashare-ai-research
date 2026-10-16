"""
决策模块与现有 Builtin Pipeline 的集成层。

提供将现有 orchestration.builtin 流程适配到新决策协议的工具函数。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from ashare_ai.agents.decision.factor import FactorDecisionProvider
from ashare_ai.agents.decision.market_state import build_market_state_from_bundle
from ashare_ai.agents.decision.models import UnifiedDecision
from ashare_ai.agents.decision.router import create_decision_router, normalize_request_mode
from ashare_ai.quant.factor_registry import load_active_factor_genome

if TYPE_CHECKING:
    from ashare_ai.core.contracts import CompositeScore
    from ashare_ai.orchestration.bundle import CanonicalDailyBundle
    from ashare_ai.scoring.factor import FactorGenome

logger = logging.getLogger(__name__)


async def generate_decision_from_composite_score(
    composite_score: CompositeScore,
    bundle: CanonicalDailyBundle,
    *,
    mode: str = "factor",
    model_version: str | None = None,
    genome: "FactorGenome | None" = None,
) -> UnifiedDecision:
    """
    从现有 CompositeScore 生成 UnifiedDecision。

    集成点：在 builtin pipeline 计算出 CompositeScore 后调用此函数。

    Args:
        composite_score: 现有评分结果
        bundle: 市场数据 Bundle
        mode: 决策模式；``factor`` 与兼容别名 ``legacy`` 都生成因子决策
        model_version: 模型版本；缺省使用因子公式版本

    Returns:
        UnifiedDecision: 统一决策结果
    """
    # 从 Bundle 构建 MarketState
    market_state = build_market_state_from_bundle(
        bundle=bundle,
        symbol=composite_score.symbol,
        trading_date=composite_score.trading_date,
    )

    # ``legacy`` 只是兼容输入别名，仍然由确定性因子公式生成决策。
    normalize_request_mode(mode)
    provider = FactorDecisionProvider(genome or load_active_factor_genome())
    if model_version:
        logger.debug("ignoring requested model_version %s; factor formula is versioned", model_version)
    return await provider.predict(market_state, composite_score=composite_score)


async def generate_decision_for_symbol(
    bundle: CanonicalDailyBundle,
    symbol: str,
    trading_date: date,
    *,
    mode: str = "factor",
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

    router = create_decision_router(
        mode=mode,
        fallback_enabled=fallback_enabled,
        genome=load_active_factor_genome(),
    )
    return await router.predict(market_state)


async def batch_generate_decisions(
    bundle: CanonicalDailyBundle,
    symbols: list[str],
    *,
    mode: str = "factor",
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
    # Resolve once for the whole batch so a mid-run approval cannot mix
    # strategy versions between symbols.
    frozen_genome = load_active_factor_genome()
    router = create_decision_router(
        mode=mode,
        fallback_enabled=fallback_enabled,
        genome=frozen_genome,
    )
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
