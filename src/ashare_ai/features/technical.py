from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from datetime import date
from itertools import pairwise

from pydantic import AwareDatetime, Field

from ashare_ai.core.contracts import (
    AdjustmentFactor,
    CanonicalSymbol,
    DailyBar,
    FrozenModel,
    TradeStatus,
)


class TechnicalFeatures(FrozenModel):
    symbol: CanonicalSymbol
    trading_date: date
    decision_at: AwareDatetime
    observations: int = Field(ge=0)
    return_5d: float | None = None
    return_20d: float | None = None
    close_to_ma20: float | None = None
    annualized_volatility_20d: float | None = None
    volume_ratio_5_to_20: float | None = None
    max_drawdown_60d: float | None = None
    completeness: float = Field(ge=0, le=1)


def extract_technical_features(
    bars: Iterable[DailyBar],
    *,
    decision_at: AwareDatetime,
    trading_date: date,
    symbol: str | None = None,
    factors: Iterable[AdjustmentFactor] = (),
) -> TechnicalFeatures:
    visible = [
        bar
        for bar in bars
        if bar.available_at <= decision_at
        and bar.trading_date <= trading_date
        and bar.trade_status == TradeStatus.TRADING
    ]
    if symbol is not None:
        visible = [bar for bar in visible if bar.symbol == symbol]
    if not visible:
        raise ValueError("no point-in-time daily bars available")
    symbols = {bar.symbol for bar in visible}
    if len(symbols) != 1:
        raise ValueError("technical extraction requires bars for exactly one symbol")
    resolved_symbol = next(iter(symbols))

    latest_by_date: dict[date, DailyBar] = {}
    for bar in visible:
        current_bar = latest_by_date.get(bar.trading_date)
        if current_bar is None or (bar.available_at, bar.source_record_id) > (
            current_bar.available_at,
            current_bar.source_record_id,
        ):
            latest_by_date[bar.trading_date] = bar
    ordered = [latest_by_date[key] for key in sorted(latest_by_date)]

    factor_by_date: dict[date, AdjustmentFactor] = {}
    for factor in factors:
        if (
            factor.symbol == resolved_symbol
            and factor.available_at <= decision_at
            and factor.trading_date <= trading_date
        ):
            current_factor = factor_by_date.get(factor.trading_date)
            if current_factor is None or (factor.available_at, factor.source_record_id) > (
                current_factor.available_at,
                current_factor.source_record_id,
            ):
                factor_by_date[factor.trading_date] = factor

    closes = [
        float(bar.close * factor_by_date[bar.trading_date].factor)
        if bar.trading_date in factor_by_date
        else float(bar.close)
        for bar in ordered
    ]
    volumes = [float(bar.volume) for bar in ordered]
    returns = [current / previous - 1 for previous, current in pairwise(closes)]

    value_count = sum(
        value is not None
        for value in (
            _period_return(closes, 5),
            _period_return(closes, 20),
            _close_to_average(closes, 20),
            _annualized_volatility(returns, 20),
            _volume_ratio(volumes),
            _max_drawdown(closes[-60:]),
        )
    )
    return TechnicalFeatures(
        symbol=resolved_symbol,
        trading_date=trading_date,
        decision_at=decision_at,
        observations=len(ordered),
        return_5d=_period_return(closes, 5),
        return_20d=_period_return(closes, 20),
        close_to_ma20=_close_to_average(closes, 20),
        annualized_volatility_20d=_annualized_volatility(returns, 20),
        volume_ratio_5_to_20=_volume_ratio(volumes),
        max_drawdown_60d=_max_drawdown(closes[-60:]),
        completeness=value_count / 6,
    )


def _period_return(values: list[float], periods: int) -> float | None:
    if len(values) <= periods or values[-periods - 1] == 0:
        return None
    return values[-1] / values[-periods - 1] - 1


def _close_to_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    average = statistics.fmean(values[-window:])
    return None if average == 0 else values[-1] / average - 1


def _annualized_volatility(returns: list[float], window: int) -> float | None:
    if len(returns) < window:
        return None
    return statistics.pstdev(returns[-window:]) * math.sqrt(252)


def _volume_ratio(values: list[float]) -> float | None:
    if len(values) < 20:
        return None
    long_average = statistics.fmean(values[-20:])
    if long_average == 0:
        return None
    return statistics.fmean(values[-5:]) / long_average


def _max_drawdown(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return drawdown
