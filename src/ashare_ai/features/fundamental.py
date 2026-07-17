from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from pydantic import AwareDatetime, Field

from ashare_ai.core.contracts import CanonicalSymbol, FinancialFact, FrozenModel


class FundamentalFeatures(FrozenModel):
    symbol: CanonicalSymbol
    trading_date: date
    decision_at: AwareDatetime
    report_period_end: date
    revenue_growth_yoy: float | None = None
    net_profit_growth_yoy: float | None = None
    return_on_equity: float | None = None
    debt_to_assets: float | None = None
    operating_cash_flow_to_profit: float | None = None
    completeness: float = Field(ge=0, le=1)


_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("REVENUE", "OPERATING_REVENUE", "TOTAL_REVENUE"),
    "net_profit": ("NET_PROFIT", "NET_PROFIT_ATTR_PARENT"),
    "equity": ("TOTAL_EQUITY", "SHAREHOLDERS_EQUITY"),
    "assets": ("TOTAL_ASSETS",),
    "liabilities": ("TOTAL_LIABILITIES",),
    "operating_cash_flow": ("OPERATING_CASH_FLOW", "NET_OPERATING_CASH_FLOW"),
    "roe": ("ROE", "RETURN_ON_EQUITY"),
    "debt_ratio": ("DEBT_TO_ASSETS", "ASSET_LIABILITY_RATIO"),
}


def extract_fundamental_features(
    facts: Iterable[FinancialFact],
    *,
    decision_at: AwareDatetime,
    trading_date: date,
    symbol: str | None = None,
) -> FundamentalFeatures:
    visible = [
        fact
        for fact in facts
        if fact.available_at <= decision_at and fact.report_period_end <= trading_date
    ]
    if symbol is not None:
        visible = [fact for fact in visible if fact.symbol == symbol]
    if not visible:
        raise ValueError("no point-in-time financial facts available")
    symbols = {fact.symbol for fact in visible}
    if len(symbols) != 1:
        raise ValueError("fundamental extraction requires records for exactly one symbol")
    resolved_symbol = next(iter(symbols))

    by_key: dict[tuple[date, str], FinancialFact] = {}
    for fact in visible:
        key = (fact.report_period_end, fact.field_code.upper())
        current_fact = by_key.get(key)
        if current_fact is None or (
            fact.revision_seq,
            fact.available_at,
            fact.source_record_id,
        ) > (
            current_fact.revision_seq,
            current_fact.available_at,
            current_fact.source_record_id,
        ):
            by_key[key] = fact

    latest_period = max(period for period, _ in by_key)
    prior_period = _previous_year(latest_period)
    current_values = _period_values(by_key, latest_period)
    prior_values = _period_values(by_key, prior_period)

    revenue = _first(current_values, "revenue")
    prior_revenue = _first(prior_values, "revenue")
    profit = _first(current_values, "net_profit")
    prior_profit = _first(prior_values, "net_profit")
    equity = _first(current_values, "equity")
    assets = _first(current_values, "assets")
    liabilities = _first(current_values, "liabilities")
    operating_cash_flow = _first(current_values, "operating_cash_flow")

    roe = _first(current_values, "roe")
    if roe is None:
        roe = _safe_divide(profit, equity)
    debt_ratio = _first(current_values, "debt_ratio")
    if debt_ratio is None:
        debt_ratio = _safe_divide(liabilities, assets)

    values = (
        revenue,
        profit,
        equity,
        assets,
        liabilities,
        operating_cash_flow,
    )
    return FundamentalFeatures(
        symbol=resolved_symbol,
        trading_date=trading_date,
        decision_at=decision_at,
        report_period_end=latest_period,
        revenue_growth_yoy=_growth(revenue, prior_revenue),
        net_profit_growth_yoy=_growth(profit, prior_profit),
        return_on_equity=_to_float(roe),
        debt_to_assets=_to_float(debt_ratio),
        operating_cash_flow_to_profit=_to_float(_safe_divide(operating_cash_flow, profit)),
        completeness=sum(value is not None for value in values) / len(values),
    )


def _previous_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _period_values(
    facts: dict[tuple[date, str], FinancialFact], period: date
) -> dict[str, Decimal | None]:
    return {
        field: fact.value for (fact_period, field), fact in facts.items() if fact_period == period
    }


def _first(values: dict[str, Decimal | None], alias_group: str) -> Decimal | None:
    for alias in _ALIASES[alias_group]:
        if alias in values:
            return values[alias]
    return None


def _safe_divide(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _growth(current: Decimal | None, prior: Decimal | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return float((current - prior) / abs(prior))


def _to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
