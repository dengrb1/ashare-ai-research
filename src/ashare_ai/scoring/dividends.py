from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ashare_ai.core.contracts import CashDividend


class DividendBonusResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cash_dividend_per_share_365d: Decimal = Field(ge=0)
    dividend_yield_365d: Decimal = Field(ge=0)
    consecutive_dividend_years: int = Field(ge=0)
    yield_bonus: float = Field(ge=0, le=8)
    continuity_bonus: float = Field(ge=0, le=2)
    total_bonus: float = Field(ge=0, le=10)
    dividend_ids: tuple[str, ...] = ()


def calculate_dividend_bonus(
    dividends: list[CashDividend] | tuple[CashDividend, ...],
    *,
    symbol: str,
    decision_at: datetime,
    frozen_close: Decimal,
    yield_multiplier: Decimal = Decimal("200"),
    yield_bonus_cap: float = 8.0,
    continuity_bonus_cap: float = 2.0,
    total_bonus_cap: float = 10.0,
) -> DividendBonusResult:
    if frozen_close <= 0:
        raise ValueError("frozen_close must be positive")
    eligible = [
        item
        for item in dividends
        if item.symbol == symbol
        and item.official_verified
        and item.available_at <= decision_at
        and item.payment_date <= decision_at.date()
    ]
    deduped: dict[tuple[int, object, Decimal], CashDividend] = {}
    for item in eligible:
        key = (item.fiscal_year, item.payment_date, item.cash_dividend_per_share)
        current = deduped.get(key)
        if current is None or (item.official_verified and not current.official_verified):
            deduped[key] = item
    eligible = list(deduped.values())

    window_start = decision_at.date() - timedelta(days=365)
    cash_365d = sum(
        (
            item.cash_dividend_per_share
            for item in eligible
            if window_start < item.payment_date <= decision_at.date()
        ),
        Decimal("0"),
    )
    dividend_yield = cash_365d / frozen_close
    yield_bonus = min(yield_bonus_cap, float(dividend_yield * yield_multiplier))

    years = sorted({item.fiscal_year for item in eligible}, reverse=True)
    consecutive_years = 0
    if years:
        expected = years[0]
        for year in years:
            if year != expected:
                break
            consecutive_years += 1
            expected -= 1
    continuity_bonus = min(continuity_bonus_cap, max(consecutive_years - 1, 0) * 0.5)
    total_bonus = min(total_bonus_cap, yield_bonus + continuity_bonus)
    return DividendBonusResult(
        cash_dividend_per_share_365d=cash_365d,
        dividend_yield_365d=dividend_yield,
        consecutive_dividend_years=consecutive_years,
        yield_bonus=round(yield_bonus, 6),
        continuity_bonus=round(continuity_bonus, 6),
        total_bonus=round(total_bonus, 6),
        dividend_ids=tuple(sorted(item.dividend_id for item in eligible)),
    )
