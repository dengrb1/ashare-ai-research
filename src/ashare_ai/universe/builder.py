from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from datetime import date
from enum import StrEnum
from itertools import pairwise

from pydantic import AwareDatetime, Field

from ashare_ai.core.contracts import (
    CanonicalSymbol,
    DailyBar,
    FrozenModel,
    SecurityMasterRecord,
    SecurityStatusRecord,
    SecurityType,
    TradeStatus,
)
from ashare_ai.core.hashing import stable_hash


class EligibilityReason(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    NO_ACTIVE_MASTER = "NO_ACTIVE_MASTER"
    NOT_STOCK = "NOT_STOCK"
    DELISTED = "DELISTED"
    ST = "ST"
    SUSPENDED = "SUSPENDED"
    TOO_NEW = "TOO_NEW"
    MISSING_STATUS = "MISSING_STATUS"
    MISSING_BAR = "MISSING_BAR"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    ABNORMAL_VOLATILITY = "ABNORMAL_VOLATILITY"


class UniverseConfig(FrozenModel):
    version: str = "all-a-v1"
    min_listing_days: int = Field(default=120, ge=0)
    liquidity_window: int = Field(default=20, gt=0)
    min_average_amount: float = Field(default=20_000_000.0, ge=0)
    min_history_days: int = Field(default=20, gt=1)
    abnormal_return_window: int = Field(default=20, gt=0)
    max_abs_daily_return: float = Field(default=0.25, gt=0)
    max_annualized_volatility: float = Field(default=1.50, gt=0)


class EligibilityDecision(FrozenModel):
    symbol: CanonicalSymbol
    eligible: bool
    reasons: tuple[EligibilityReason, ...]
    average_amount: float | None = None
    annualized_volatility: float | None = None
    history_days: int = Field(ge=0)


class UniverseResult(FrozenModel):
    trading_date: date
    decision_at: AwareDatetime
    version: str
    included: tuple[CanonicalSymbol, ...]
    decisions: tuple[EligibilityDecision, ...]
    input_hash: str


def build_dynamic_universe(
    masters: Iterable[SecurityMasterRecord],
    statuses: Iterable[SecurityStatusRecord],
    bars: Iterable[DailyBar],
    *,
    trading_date: date,
    decision_at: AwareDatetime,
    config: UniverseConfig | None = None,
    trading_calendar: Sequence[date] | None = None,
) -> UniverseResult:
    config = config or UniverseConfig()
    visible_masters = [
        item
        for item in masters
        if item.available_at <= decision_at and item.trading_date <= trading_date
    ]
    visible_statuses = [
        item
        for item in statuses
        if item.available_at <= decision_at and item.trading_date <= trading_date
    ]
    visible_bars = [
        item
        for item in bars
        if item.available_at <= decision_at and item.trading_date <= trading_date
    ]
    symbols = sorted(
        {item.symbol for item in visible_masters}
        | {item.symbol for item in visible_statuses}
        | {item.symbol for item in visible_bars}
    )
    master_by_symbol = _active_masters(visible_masters, trading_date)
    status_by_symbol = _active_statuses(visible_statuses, trading_date)
    bars_by_symbol = _bars_by_symbol(visible_bars)

    decisions = tuple(
        _evaluate(
            symbol,
            master_by_symbol.get(symbol),
            status_by_symbol.get(symbol),
            bars_by_symbol.get(symbol, ()),
            trading_date=trading_date,
            config=config,
            trading_calendar=trading_calendar,
        )
        for symbol in symbols
    )
    included = tuple(decision.symbol for decision in decisions if decision.eligible)
    return UniverseResult(
        trading_date=trading_date,
        decision_at=decision_at,
        version=config.version,
        included=included,
        decisions=decisions,
        input_hash=stable_hash(
            {
                "masters": visible_masters,
                "statuses": visible_statuses,
                "bars": visible_bars,
                "trading_date": trading_date,
                "decision_at": decision_at,
                "config": config,
                "trading_calendar": trading_calendar,
            }
        ),
    )


def _evaluate(
    symbol: str,
    master: SecurityMasterRecord | None,
    status: SecurityStatusRecord | None,
    bars: tuple[DailyBar, ...],
    *,
    trading_date: date,
    config: UniverseConfig,
    trading_calendar: Sequence[date] | None,
) -> EligibilityDecision:
    reasons: list[EligibilityReason] = []
    if master is None:
        reasons.append(EligibilityReason.NO_ACTIVE_MASTER)
    else:
        if master.security_type != SecurityType.STOCK:
            reasons.append(EligibilityReason.NOT_STOCK)
        if master.delist_date is not None and master.delist_date <= trading_date:
            reasons.append(EligibilityReason.DELISTED)
        listing_days = _listing_days(master.list_date, trading_date, trading_calendar)
        if listing_days < config.min_listing_days:
            reasons.append(EligibilityReason.TOO_NEW)

    if status is None:
        reasons.append(EligibilityReason.MISSING_STATUS)
    else:
        if status.is_st:
            reasons.append(EligibilityReason.ST)
        if status.is_suspended:
            reasons.append(EligibilityReason.SUSPENDED)

    latest_bar = bars[-1] if bars else None
    if latest_bar is None or latest_bar.trading_date != trading_date:
        reasons.append(EligibilityReason.MISSING_BAR)
    elif latest_bar.trade_status != TradeStatus.TRADING:
        reasons.append(EligibilityReason.SUSPENDED)

    history_days = len(bars)
    if history_days < config.min_history_days:
        reasons.append(EligibilityReason.INSUFFICIENT_HISTORY)

    liquidity_bars = bars[-config.liquidity_window :]
    average_amount = (
        statistics.fmean(float(bar.amount) for bar in liquidity_bars)
        if len(liquidity_bars) >= config.liquidity_window
        else None
    )
    if average_amount is None or average_amount < config.min_average_amount:
        reasons.append(EligibilityReason.LOW_LIQUIDITY)

    daily_returns = _daily_returns(bars)
    annualized_volatility = (
        statistics.pstdev(daily_returns[-config.liquidity_window :]) * math.sqrt(252)
        if len(daily_returns) >= config.liquidity_window
        else None
    )
    recent_abnormal_returns = daily_returns[-config.abnormal_return_window :]
    if any(abs(value) > config.max_abs_daily_return for value in recent_abnormal_returns):
        reasons.append(EligibilityReason.ABNORMAL_VOLATILITY)
    if (
        annualized_volatility is not None
        and annualized_volatility > config.max_annualized_volatility
    ):
        reasons.append(EligibilityReason.ABNORMAL_VOLATILITY)

    unique_reasons = tuple(dict.fromkeys(reasons))
    return EligibilityDecision(
        symbol=symbol,
        eligible=not unique_reasons,
        reasons=unique_reasons or (EligibilityReason.ELIGIBLE,),
        average_amount=average_amount,
        annualized_volatility=annualized_volatility,
        history_days=history_days,
    )


def _active_masters(
    records: list[SecurityMasterRecord], trading_date: date
) -> dict[str, SecurityMasterRecord]:
    active: dict[str, SecurityMasterRecord] = {}
    for item in records:
        if item.effective_from > trading_date:
            continue
        if item.effective_to is not None and item.effective_to < trading_date:
            continue
        current = active.get(item.symbol)
        if current is None or (item.effective_from, item.available_at, item.source_record_id) > (
            current.effective_from,
            current.available_at,
            current.source_record_id,
        ):
            active[item.symbol] = item
    return active


def _active_statuses(
    records: list[SecurityStatusRecord], trading_date: date
) -> dict[str, SecurityStatusRecord]:
    active: dict[str, SecurityStatusRecord] = {}
    for item in records:
        if item.effective_from > trading_date:
            continue
        if item.effective_to is not None and item.effective_to < trading_date:
            continue
        current = active.get(item.symbol)
        if current is None or (item.effective_from, item.available_at, item.source_record_id) > (
            current.effective_from,
            current.available_at,
            current.source_record_id,
        ):
            active[item.symbol] = item
    return active


def _bars_by_symbol(records: list[DailyBar]) -> dict[str, tuple[DailyBar, ...]]:
    latest: dict[tuple[str, date], DailyBar] = {}
    for item in records:
        key = (item.symbol, item.trading_date)
        current = latest.get(key)
        if current is None or (item.available_at, item.source_record_id) > (
            current.available_at,
            current.source_record_id,
        ):
            latest[key] = item
    grouped: dict[str, list[DailyBar]] = {}
    for item in latest.values():
        grouped.setdefault(item.symbol, []).append(item)
    return {
        symbol: tuple(sorted(items, key=lambda item: item.trading_date))
        for symbol, items in grouped.items()
    }


def _listing_days(
    list_date: date, trading_date: date, trading_calendar: Sequence[date] | None
) -> int:
    if trading_calendar is None:
        return max((trading_date - list_date).days, 0)
    return sum(list_date <= session <= trading_date for session in trading_calendar)


def _daily_returns(bars: tuple[DailyBar, ...]) -> list[float]:
    returns: list[float] = []
    for previous, current in pairwise(bars):
        base = float(current.prev_close or previous.close)
        if base > 0:
            returns.append(float(current.close) / base - 1)
    return returns
