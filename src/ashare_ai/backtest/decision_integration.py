"""
回测引擎集成 - 调用 DecisionRouter。

扩展 BacktestEngine 支持决策模式：
- 从 DecisionRouter 生成信号
- 对比 Legacy vs Jev 模式
- 生成决策元数据
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from ashare_ai.agents.decision.backtest_adapter import decision_to_signal
from ashare_ai.agents.decision.market_state import build_market_state_from_bundle
from ashare_ai.agents.decision.router import create_decision_router
from ashare_ai.backtest.engine import BacktestSignal

if TYPE_CHECKING:
    from ashare_ai.orchestration.bundle import CanonicalDailyBundle

logger = logging.getLogger(__name__)


async def generate_backtest_signals_from_decisions(
    bundle: CanonicalDailyBundle,
    symbols: list[str],
    signal_date: date,
    *,
    mode: str = "legacy",
    fallback_enabled: bool = False,
) -> list[BacktestSignal]:
    """
    使用 DecisionRouter 生成回测信号。

    Args:
        bundle: 市场数据 Bundle
        symbols: 股票列表
        signal_date: 信号日期
        mode: 决策模式
        fallback_enabled: 是否启用 fallback

    Returns:
        list[BacktestSignal]: 回测信号列表
    """
    router = create_decision_router(mode=mode, fallback_enabled=fallback_enabled)
    signals: list[BacktestSignal] = []

    for symbol in symbols:
        try:
            # 构建市场状态
            market_state = build_market_state_from_bundle(
                bundle=bundle,
                symbol=symbol,
                trading_date=signal_date,
            )

            # 生成决策
            decision = await router.predict(market_state)

            # 转换为回测信号
            signal = decision_to_signal(
                decision=decision,
                signal_date=signal_date,
                snapshot_hash=bundle.snapshot_hash,
            )

            signals.append(signal)

        except Exception as e:
            logger.error(f"Failed to generate decision signal for {symbol}: {e}")
            # Fail-closed: 跳过该股票
            continue

    return signals


async def compare_decision_modes(
    bundle: CanonicalDailyBundle,
    symbols: list[str],
    signal_date: date,
) -> dict[str, list[BacktestSignal]]:
    """
    对比 Legacy 和 Jev 模式生成的信号。

    Args:
        bundle: 市场数据 Bundle
        symbols: 股票列表
        signal_date: 信号日期

    Returns:
        dict: {"legacy": [...], "jev": [...]}
    """
    legacy_signals = await generate_backtest_signals_from_decisions(
        bundle=bundle,
        symbols=symbols,
        signal_date=signal_date,
        mode="legacy",
    )

    jev_signals = await generate_backtest_signals_from_decisions(
        bundle=bundle,
        symbols=symbols,
        signal_date=signal_date,
        mode="jev",
    )

    return {
        "legacy": legacy_signals,
        "jev": jev_signals,
    }
