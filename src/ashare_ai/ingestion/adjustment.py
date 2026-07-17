from __future__ import annotations

from datetime import date
from decimal import Decimal

from ashare_ai.core.contracts import AdjustmentFactor, DailyBar, FrozenModel


class AdjustedOHLC(FrozenModel):
    symbol: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    factor: Decimal
    reference_factor: Decimal


def adjust_price(
    raw_price: Decimal | str | int | float,
    factor: Decimal | str | int | float,
    *,
    reference_factor: Decimal | str | int | float = Decimal("1"),
) -> Decimal:
    price_value = Decimal(str(raw_price))
    factor_value = Decimal(str(factor))
    reference_value = Decimal(str(reference_factor))
    if price_value <= 0:
        raise ValueError("raw price must be positive")
    if factor_value <= 0 or reference_value <= 0:
        raise ValueError("adjustment factors must be positive")
    return price_value * factor_value / reference_value


def adjust_daily_bar(
    bar: DailyBar,
    factor: AdjustmentFactor,
    *,
    reference_factor: Decimal | str | int | float = Decimal("1"),
) -> AdjustedOHLC:
    if (bar.symbol, bar.trading_date) != (factor.symbol, factor.trading_date):
        raise ValueError("bar and adjustment factor must have the same symbol and trading_date")
    reference_value = Decimal(str(reference_factor))
    return AdjustedOHLC(
        symbol=bar.symbol,
        trading_date=bar.trading_date,
        open=adjust_price(bar.open, factor.factor, reference_factor=reference_value),
        high=adjust_price(bar.high, factor.factor, reference_factor=reference_value),
        low=adjust_price(bar.low, factor.factor, reference_factor=reference_value),
        close=adjust_price(bar.close, factor.factor, reference_factor=reference_value),
        factor=factor.factor,
        reference_factor=reference_value,
    )
