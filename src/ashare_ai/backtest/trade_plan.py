from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from itertools import product
from math import sqrt
from statistics import fmean, pstdev
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_ai.core.contracts import Side
from ashare_ai.trading.execution import (
    AccountState,
    DailyExecutionModel,
    ExecutionBar,
    ExecutionConfig,
    Order,
    OrderStatus,
    PositionLot,
)
from ashare_ai.trading.rules import TradingRule

OPTIMIZER_VERSION = "trade-plan-grid-oos-v1"


class _PreparedBuy(NamedTuple):
    buy_index: int
    entry_price: Decimal
    quantity: int
    cash_after_buy: Decimal
    acquired_date: date
    sellable_date: date
    unit_cost: Decimal


class TradePlanOutcome(StrEnum):
    BUY = "BUY"
    NO_BUY = "NO_BUY"


class StrategyParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_discount: Decimal = Field(ge=0, le=Decimal("0.10"))
    take_profit: Decimal = Field(gt=0, lt=1)
    stop_loss: Decimal = Field(gt=0, lt=1)
    trailing_stop: Decimal = Field(gt=0, lt=1)
    max_holding_sessions: int = Field(gt=0)
    entry_valid_sessions: int = Field(default=3, ge=1)


class StrategyMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    net_return: float
    sharpe: float
    maximum_drawdown: float = Field(ge=0)
    completed_trades: int = Field(ge=0)
    fill_count: int = Field(ge=0)


class OptimizedStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    outcome: TradePlanOutcome
    reason_code: str | None = None
    parameters: StrategyParameters | None = None
    training_metrics: StrategyMetrics | None = None
    validation_metrics: StrategyMetrics | None = None
    history_sessions: int = Field(ge=0)
    training_sessions: int = Field(ge=0)
    validation_sessions: int = Field(ge=0)
    optimizer_version: str = OPTIMIZER_VERSION

    @model_validator(mode="after")
    def require_strategy_for_buy(self) -> OptimizedStrategy:
        if self.outcome == TradePlanOutcome.BUY and (
            self.parameters is None or self.validation_metrics is None
        ):
            raise ValueError("BUY outcome requires deterministic parameters and metrics")
        return self


class TradePlanCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    industry_code: str
    volatility: float = Field(gt=0)
    event_risk_multiplier: float = Field(gt=0, le=1)


class TradePlanPositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    quantity: int = Field(ge=0)


class TradePlanSymbolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    outcome: TradePlanOutcome
    action: str
    reason_code: str | None = None
    reference_price: Decimal = Field(gt=0)
    reference_date: date
    entry_valid_from: date
    entry_valid_until: date
    limit_price_low: Decimal = Field(gt=0)
    limit_price_high: Decimal = Field(gt=0)
    existing_quantity: int = Field(ge=0)
    suggested_additional_quantity: int = Field(ge=0)
    target_total_quantity: int = Field(ge=0)
    target_weight: float = Field(ge=0, le=1)
    estimated_cash_usage: Decimal = Field(ge=0)
    estimated_fees: Decimal = Field(ge=0)
    earliest_sell_date: date
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    trailing_stop: Decimal | None = Field(default=None, gt=0, lt=1)
    maximum_holding_sessions: int | None = Field(default=None, gt=0)
    score_exit_threshold: float = Field(default=60, ge=0, le=100)
    cancellation_rules: tuple[str, ...]
    strategy: OptimizedStrategy


class DeterministicTradePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = "RISK_ADJUSTED_RETURN"
    outcome: TradePlanOutcome
    trading_date: date
    decision_at: str
    total_assets: Decimal = Field(gt=0)
    investable_budget: Decimal = Field(ge=0)
    available_cash: Decimal = Field(ge=0)
    retained_cash: Decimal = Field(ge=0)
    symbol_plans: tuple[TradePlanSymbolDecision, ...]
    optimizer_version: str = OPTIMIZER_VERSION


def build_deterministic_trade_plan(
    *,
    trading_date: date,
    decision_at: str,
    candidates: Sequence[TradePlanCandidateInput],
    positions: Sequence[TradePlanPositionInput],
    strategies: Mapping[str, OptimizedStrategy],
    reference_prices: Mapping[str, Decimal],
    effective_rules: Mapping[str, TradingRule],
    future_trading_dates: Sequence[date],
    total_assets: Decimal,
    available_cash: Decimal,
    requested_budget: Decimal,
    maximum_single_weight: Decimal = Decimal("0.08"),
    maximum_industry_weight: Decimal = Decimal("0.25"),
    score_exit_threshold: Decimal = Decimal("60"),
) -> DeterministicTradePlan:
    if len(future_trading_dates) < 3:
        raise ValueError("trade plan requires three frozen future trading dates")
    position_by_symbol = {item.symbol: item.quantity for item in positions}
    investable = max(Decimal("0"), min(requested_budget, available_cash))
    eligible = [
        item
        for item in candidates
        if strategies[item.symbol].outcome == TradePlanOutcome.BUY
    ]
    factors = {
        item.symbol: Decimal(str(item.event_risk_multiplier / max(item.volatility, 0.01)))
        for item in eligible
    }
    factor_total = sum(factors.values(), Decimal("0"))
    industry_allocated: dict[str, Decimal] = {}
    retained = investable
    plans: list[TradePlanSymbolDecision] = []
    for candidate in candidates:
        symbol = candidate.symbol
        strategy = strategies[symbol]
        price = reference_prices[symbol]
        rule = effective_rules[symbol]
        existing_quantity = position_by_symbol.get(symbol, 0)
        existing_value = price * existing_quantity
        single_cap = total_assets * maximum_single_weight
        industry_cap = total_assets * maximum_industry_weight
        if strategy.outcome == TradePlanOutcome.BUY and factor_total > 0:
            proportional = investable * factors[symbol] / factor_total
            industry_room = max(
                Decimal("0"),
                industry_cap - industry_allocated.get(candidate.industry_code, Decimal("0")),
            )
            target_value = min(
                single_cap,
                existing_value + proportional,
                existing_value + industry_room,
            )
            additional_budget = max(Decimal("0"), target_value - existing_value)
        else:
            additional_budget = Decimal("0")
        parameters = strategy.parameters
        discount = parameters.entry_discount if parameters is not None else Decimal("0")
        limit_low = _money(price * (Decimal("1") - discount))
        limit_high = _money(price)
        lot = int(rule.details.get("buy_qty_step", rule.lot_size))
        provisional_quantity = int((additional_budget / limit_high) // lot) * lot
        quantity, fees, cash_usage = _fit_quantity_with_fees(
            quantity=provisional_quantity,
            lot=lot,
            price=limit_high,
            budget=min(additional_budget, retained),
            rule=rule,
        )
        retained -= cash_usage
        industry_allocated[candidate.industry_code] = (
            industry_allocated.get(candidate.industry_code, Decimal("0")) + cash_usage
        )
        target_quantity = existing_quantity + quantity
        if strategy.outcome == TradePlanOutcome.NO_BUY:
            action = "NO_BUY"
            reason_code = strategy.reason_code
        elif existing_value >= single_cap:
            action = "REVIEW_NO_ADD"
            reason_code = "EXISTING_POSITION_AT_OR_ABOVE_TARGET"
            quantity = 0
            fees = Decimal("0")
            cash_usage = Decimal("0")
            target_quantity = existing_quantity
        elif quantity <= 0:
            action = "NO_BUY"
            reason_code = "BUDGET_BELOW_EFFECTIVE_LOT"
        else:
            action = "ADD"
            reason_code = None
        take_price = (
            _money(price * (Decimal("1") + parameters.take_profit))
            if parameters is not None
            else None
        )
        stop_price = (
            _money(price * (Decimal("1") - parameters.stop_loss))
            if parameters is not None
            else None
        )
        plans.append(
            TradePlanSymbolDecision(
                symbol=symbol,
                outcome=(
                    TradePlanOutcome.BUY if action == "ADD" else TradePlanOutcome.NO_BUY
                ),
                action=action,
                reason_code=reason_code,
                reference_price=price,
                reference_date=trading_date,
                entry_valid_from=future_trading_dates[0],
                entry_valid_until=future_trading_dates[2],
                limit_price_low=limit_low,
                limit_price_high=limit_high,
                existing_quantity=existing_quantity,
                suggested_additional_quantity=quantity,
                target_total_quantity=target_quantity,
                target_weight=min(1.0, float(price * target_quantity / total_assets)),
                estimated_cash_usage=cash_usage,
                estimated_fees=fees,
                earliest_sell_date=future_trading_dates[1],
                take_profit_price=take_price,
                stop_loss_price=stop_price,
                trailing_stop=parameters.trailing_stop if parameters is not None else None,
                maximum_holding_sessions=(
                    parameters.max_holding_sessions if parameters is not None else None
                ),
                score_exit_threshold=float(score_exit_threshold),
                cancellation_rules=(
                    f"评分跌破 {score_exit_threshold} 时取消尚未成交的买入并复评持仓",
                    "事件风险乘数恶化时取消买入并触发退出复评",
                    "出现 CRITICAL 官方事件时立即取消买入并按规则退出",
                ),
                strategy=strategy,
            )
        )
    outcome = (
        TradePlanOutcome.BUY
        if any(item.outcome == TradePlanOutcome.BUY for item in plans)
        else TradePlanOutcome.NO_BUY
    )
    return DeterministicTradePlan(
        outcome=outcome,
        trading_date=trading_date,
        decision_at=decision_at,
        total_assets=total_assets,
        investable_budget=investable,
        available_cash=available_cash,
        retained_cash=max(Decimal("0"), retained),
        symbol_plans=tuple(plans),
    )


def optimize_trade_strategy(
    *,
    symbol: str,
    trading_calendar: Sequence[date],
    bars: Sequence[ExecutionBar],
    rules: Mapping[date, TradingRule],
    adv_amounts: Mapping[date, Decimal],
    volatilities: Mapping[date, float],
    execution_config: ExecutionConfig,
    maximum_drawdown: float = 0.12,
    minimum_completed_trades: int = 5,
    maximum_history_sessions: int = 240,
    training_sessions: int = 160,
    validation_sessions: int = 80,
    entry_discounts: Sequence[Decimal] = (
        Decimal("0"),
        Decimal("0.01"),
        Decimal("0.02"),
    ),
    take_profits: Sequence[Decimal] = (
        Decimal("0.08"),
        Decimal("0.12"),
        Decimal("0.16"),
    ),
    stop_losses: Sequence[Decimal] = (
        Decimal("0.05"),
        Decimal("0.08"),
        Decimal("0.10"),
    ),
    trailing_stops: Sequence[Decimal] = (Decimal("0.05"), Decimal("0.08")),
    maximum_holding_options: Sequence[int] = (10, 20, 40, 60),
    entry_valid_sessions: int = 3,
    entry_step_sessions: int = 10,
) -> OptimizedStrategy:
    if training_sessions <= 0 or validation_sessions <= 0:
        raise ValueError("training and validation sessions must be positive")
    if training_sessions + validation_sessions > maximum_history_sessions:
        raise ValueError("training and validation sessions exceed maximum history")
    calendar = tuple(sorted(set(trading_calendar)))[-maximum_history_sessions:]
    bar_by_date = {
        item.trading_date: item
        for item in bars
        if item.symbol == symbol and item.trading_date in set(calendar)
    }
    calendar = tuple(value for value in calendar if value in bar_by_date and value in rules)
    history_count = len(calendar)
    if history_count < 120:
        return OptimizedStrategy(
            symbol=symbol,
            outcome=TradePlanOutcome.NO_BUY,
            reason_code="INSUFFICIENT_HISTORY",
            history_sessions=history_count,
            training_sessions=0,
            validation_sessions=0,
        )

    validation_count = (
        validation_sessions
        if history_count >= training_sessions + validation_sessions
        else min(validation_sessions, max(40, history_count // 3))
    )
    training_count = history_count - validation_count
    training_calendar = calendar[:training_count]
    validation_calendar = calendar[training_count:]
    execution = DailyExecutionModel(execution_config)
    buy_cache: dict[tuple[date, Decimal, int], _PreparedBuy | None] = {}
    exit_cache: dict[tuple[object, ...], float | None] = {}
    metrics_cache: dict[tuple[float, ...], StrategyMetrics] = {}
    path_cache: dict[tuple[date, Decimal, int], tuple[tuple[date, ExecutionBar, Decimal], ...]] = {}
    passing: list[tuple[OptimizedStrategy, tuple[float, float, float]]] = []
    for values in product(
        entry_discounts,
        take_profits,
        stop_losses,
        trailing_stops,
        maximum_holding_options,
    ):
        parameters = StrategyParameters(
            entry_discount=values[0],
            take_profit=values[1],
            stop_loss=values[2],
            trailing_stop=values[3],
            max_holding_sessions=values[4],
            entry_valid_sessions=entry_valid_sessions,
        )
        training = _simulate_parameter_set(
            symbol=symbol,
            calendar=training_calendar,
            bars=bar_by_date,
            rules=rules,
            adv_amounts=adv_amounts,
            volatilities=volatilities,
            execution=execution,
            parameters=parameters,
            entry_step_sessions=entry_step_sessions,
            buy_cache=buy_cache,
            exit_cache=exit_cache,
            metrics_cache=metrics_cache,
            path_cache=path_cache,
        )
        validation = _simulate_parameter_set(
            symbol=symbol,
            calendar=validation_calendar,
            bars=bar_by_date,
            rules=rules,
            adv_amounts=adv_amounts,
            volatilities=volatilities,
            execution=execution,
            parameters=parameters,
            entry_step_sessions=entry_step_sessions,
            buy_cache=buy_cache,
            exit_cache=exit_cache,
            metrics_cache=metrics_cache,
            path_cache=path_cache,
        )
        if (
            validation.net_return <= 0
            or validation.maximum_drawdown > maximum_drawdown
            or validation.completed_trades < minimum_completed_trades
        ):
            continue
        strategy = OptimizedStrategy(
            symbol=symbol,
            outcome=TradePlanOutcome.BUY,
            parameters=parameters,
            training_metrics=training,
            validation_metrics=validation,
            history_sessions=history_count,
            training_sessions=training_count,
            validation_sessions=validation_count,
        )
        passing.append(
            (
                strategy,
                (
                    validation.sharpe,
                    validation.net_return,
                    -validation.maximum_drawdown,
                ),
            )
        )
    if not passing:
        return OptimizedStrategy(
            symbol=symbol,
            outcome=TradePlanOutcome.NO_BUY,
            reason_code="NO_STRATEGY_PASSED",
            history_sessions=history_count,
            training_sessions=training_count,
            validation_sessions=validation_count,
        )
    return max(passing, key=lambda item: item[1])[0]


def _simulate_parameter_set(
    *,
    symbol: str,
    calendar: Sequence[date],
    bars: Mapping[date, ExecutionBar],
    rules: Mapping[date, TradingRule],
    adv_amounts: Mapping[date, Decimal],
    volatilities: Mapping[date, float],
    execution: DailyExecutionModel,
    parameters: StrategyParameters,
    entry_step_sessions: int = 10,
    buy_cache: dict[tuple[date, Decimal, int], _PreparedBuy | None] | None = None,
    exit_cache: dict[tuple[object, ...], float | None] | None = None,
    metrics_cache: dict[tuple[float, ...], StrategyMetrics] | None = None,
    path_cache: dict[
        tuple[date, Decimal, int], tuple[tuple[date, ExecutionBar, Decimal], ...]
    ] | None = None,
) -> StrategyMetrics:
    returns: list[float] = []
    fill_count = 0
    for entry_index in range(0, max(0, len(calendar) - 1), entry_step_sessions):
        outcome = _simulate_one_entry(
            symbol=symbol,
            calendar=calendar,
            entry_index=entry_index,
            bars=bars,
            rules=rules,
            adv_amounts=adv_amounts,
            volatilities=volatilities,
            execution=execution,
            parameters=parameters,
            buy_cache=buy_cache,
            exit_cache=exit_cache,
            path_cache=path_cache,
        )
        if outcome is None:
            continue
        fill_count += 1
        returns.append(outcome)
    returns_key = tuple(returns)
    if metrics_cache is not None and returns_key in metrics_cache:
        return metrics_cache[returns_key]
    if not returns:
        metrics = StrategyMetrics(
            net_return=0,
            sharpe=0,
            maximum_drawdown=0,
            completed_trades=0,
            fill_count=0,
        )
    else:
        mean_return = fmean(returns)
        deviation = pstdev(returns) if len(returns) > 1 else 0.0
        sharpe = mean_return / deviation * sqrt(25.2) if deviation > 0 else 0.0
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for value in returns:
            equity *= 1 + value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        metrics = StrategyMetrics(
            net_return=round(equity - 1, 8),
            sharpe=round(sharpe, 8),
            maximum_drawdown=round(max_drawdown, 8),
            completed_trades=len(returns),
            fill_count=fill_count,
        )
    if metrics_cache is not None:
        metrics_cache[returns_key] = metrics
    return metrics


def _simulate_one_entry(
    *,
    symbol: str,
    calendar: Sequence[date],
    entry_index: int,
    bars: Mapping[date, ExecutionBar],
    rules: Mapping[date, TradingRule],
    adv_amounts: Mapping[date, Decimal],
    volatilities: Mapping[date, float],
    execution: DailyExecutionModel,
    parameters: StrategyParameters,
    buy_cache: dict[tuple[date, Decimal, int], _PreparedBuy | None] | None = None,
    exit_cache: dict[tuple[object, ...], float | None] | None = None,
    path_cache: dict[
        tuple[date, Decimal, int], tuple[tuple[date, ExecutionBar, Decimal], ...]
    ] | None = None,
) -> float | None:
    signal_date = calendar[entry_index]
    initial_cash = Decimal("100000")
    buy_key = (signal_date, parameters.entry_discount, parameters.entry_valid_sessions)
    prepared = buy_cache.get(buy_key) if buy_cache is not None else None
    if buy_cache is None or buy_key not in buy_cache:
        reference = bars[signal_date].close
        limit_price = _money(reference * (Decimal("1") - parameters.entry_discount))
        buy_date: date | None = None
        buy_result = None
        buy_account = AccountState.model_construct(cash=initial_cash, lots=[])
        for offset in range(1, parameters.entry_valid_sessions + 1):
            if entry_index + offset >= len(calendar):
                break
            current_date = calendar[entry_index + offset]
            bar = bars[current_date]
            rule = rules[current_date]
            lot = int(rule.details.get("buy_qty_step", rule.lot_size))
            quantity = int((initial_cash * Decimal("0.95") / limit_price) // lot) * lot
            if quantity <= 0 or bar.low > limit_price:
                continue
            next_date = _next_date(calendar, entry_index + offset)
            if next_date is None:
                break
            reference_open = min(max(limit_price, bar.low), bar.high)
            result = execution.execute_order(
                order=Order.model_construct(
                    order_id=f"tp-buy-{signal_date}-{current_date}",
                    symbol=symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    limit_price=limit_price,
                ),
                bar=bar,
                rule=rule,
                account=buy_account,
                next_trading_date=next_date,
                adv_amount=adv_amounts.get(current_date, Decimal("0")),
                volatility=volatilities.get(current_date, 0.0),
                reference_open=reference_open if bar.open > limit_price else None,
            )
            if result.status in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
                buy_date = current_date
                buy_result = result
                break
        if buy_date is not None and buy_result is not None and buy_result.fill_price is not None:
            lot_state = buy_account.lots[0]
            prepared = _PreparedBuy(
                calendar.index(buy_date),
                buy_result.fill_price,
                buy_result.filled_quantity,
                buy_account.cash,
                lot_state.acquired_date,
                lot_state.sellable_date,
                lot_state.unit_cost,
            )
        if buy_cache is not None:
            buy_cache[buy_key] = prepared
    if prepared is None:
        return None
    buy_index = prepared.buy_index
    entry_price = prepared.entry_price
    quantity = prepared.quantity
    account: AccountState | None = None
    sell_result = None
    take_profit = parameters.take_profit
    stop_loss = parameters.stop_loss
    trailing_stop = parameters.trailing_stop
    max_holding_sessions = parameters.max_holding_sessions
    calendar_length = len(calendar)
    path = path_cache.get(buy_key) if path_cache is not None else None
    if path is None:
        highest = entry_price
        built_path: list[tuple[date, ExecutionBar, Decimal]] = []
        for current_index in range(buy_index + 1, calendar_length):
            current_date = calendar[current_index]
            bar = bars[current_date]
            highest = max(highest, bar.high)
            built_path.append((current_date, bar, highest))
        path = tuple(built_path)
        if path_cache is not None:
            path_cache[buy_key] = path
    for holding_offset, (current_date, bar, highest) in enumerate(
        path[:max_holding_sessions], start=1
    ):
        current_index = buy_index + holding_offset
        stop_price = entry_price * (Decimal("1") - stop_loss)
        take_price = entry_price * (Decimal("1") + take_profit)
        trailing_price = highest * (Decimal("1") - trailing_stop)
        hit_stop = bar.low <= stop_price
        hit_take = bar.high >= take_price
        hit_trailing = holding_offset > 1 and bar.low <= trailing_price
        trigger_price: Decimal | None = None
        if hit_stop:
            trigger_price = stop_price
        elif hit_trailing:
            trigger_price = trailing_price
        elif hit_take:
            trigger_price = take_price
        forced = (
            holding_offset == max_holding_sessions
            or current_index == calendar_length - 1
        )
        if trigger_price is None and not forced:
            continue
        attempt_key = (
            buy_key,
            current_date,
            trigger_price,
            quantity,
        )
        if exit_cache is not None and attempt_key in exit_cache:
            cached_outcome = exit_cache[attempt_key]
            if cached_outcome is not None:
                return cached_outcome
            continue
        if account is None:
            account = AccountState.model_construct(
                cash=prepared.cash_after_buy,
                lots=[
                    PositionLot.model_construct(
                        symbol=symbol,
                        quantity=quantity,
                        acquired_date=prepared.acquired_date,
                        sellable_date=prepared.sellable_date,
                        unit_cost=prepared.unit_cost,
                    )
                ],
            )
        sell_result = execution.execute_order(
            order=Order.model_construct(
                order_id=f"tp-sell-{signal_date}-{current_date}",
                symbol=symbol,
                side=Side.SELL,
                quantity=quantity,
                limit_price=None,
            ),
            bar=bar,
            rule=rules[current_date],
            account=account,
            next_trading_date=_next_date(calendar, current_index) or current_date,
            adv_amount=adv_amounts.get(current_date, Decimal("0")),
            volatility=volatilities.get(current_date, 0.0),
            reference_open=(
                None
                if trigger_price is None
                else min(max(trigger_price, bar.low), bar.high)
            ),
        )
        if sell_result.status in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
            outcome = float(account.cash / initial_cash - Decimal("1"))
            if exit_cache is not None:
                exit_cache[attempt_key] = outcome
            break
        if exit_cache is not None:
            exit_cache[attempt_key] = None
        sell_result = None
    if sell_result is None:
        return None
    assert account is not None
    return float(account.cash / initial_cash - Decimal("1"))


def _next_date(calendar: Sequence[date], index: int) -> date | None:
    return calendar[index + 1] if index + 1 < len(calendar) else None


def _fit_quantity_with_fees(
    *,
    quantity: int,
    lot: int,
    price: Decimal,
    budget: Decimal,
    rule: TradingRule,
) -> tuple[int, Decimal, Decimal]:
    current = quantity
    while current > 0:
        notional = price * current
        commission = max(rule.minimum_commission, notional * rule.commission_rate)
        transfer = notional * rule.transfer_fee_rate
        fees = _money(commission + transfer)
        cash_usage = _money(notional + fees)
        if cash_usage <= budget:
            return current, fees, cash_usage
        current -= lot
    return 0, Decimal("0"), Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
