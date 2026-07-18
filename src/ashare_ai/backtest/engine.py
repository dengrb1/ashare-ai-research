from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from itertools import pairwise
from math import sqrt

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ashare_ai.backtest.metrics import PerformanceMetrics, calculate_performance_metrics
from ashare_ai.core.contracts import Side
from ashare_ai.core.hashing import stable_hash
from ashare_ai.trading.execution import (
    AccountState,
    DailyExecutionModel,
    ExecutionBar,
    ExecutionConfig,
    ExecutionResult,
    Order,
)
from ashare_ai.trading.rules import TradingRule


class FrozenInputManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_id: str
    frozen_at: AwareDatetime
    snapshot_hashes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_hashes(self) -> FrozenInputManifest:
        if len(set(self.snapshot_hashes)) != len(self.snapshot_hashes):
            raise ValueError("snapshot_hashes must be unique")
        if any(len(value) != 64 for value in self.snapshot_hashes):
            raise ValueError("snapshot hashes must be 64-character SHA-256 values")
        return self

    @property
    def manifest_hash(self) -> str:
        return stable_hash(self)


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_cash: Decimal = Field(gt=0)
    annualization_sessions: int = Field(gt=0)
    annual_risk_free_rate: float
    execution: ExecutionConfig
    config_version: str
    input_manifest: FrozenInputManifest
    required_benchmarks: tuple[str, ...] = Field(min_length=3, max_length=3)
    capacity_scale_factors: tuple[Decimal, ...] = Field(min_length=1)
    capacity_max_participation: Decimal = Field(gt=0, le=1)
    capacity_max_slippage_bps: Decimal = Field(ge=0)
    capacity_min_fill_rate: Decimal = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_config(self) -> BacktestConfig:
        if len(set(self.required_benchmarks)) != 3:
            raise ValueError("three distinct benchmark names are required")
        if any(scale <= 0 for scale in self.capacity_scale_factors):
            raise ValueError("capacity scale factors must be positive")
        return self


class BacktestSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_date: date
    decision_at: AwareDatetime
    snapshot_hash: str = Field(min_length=64, max_length=64)
    symbol: str
    industry_code: str
    target_weight: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def decision_date_matches(self) -> BacktestSignal:
        if self.decision_at.date() != self.signal_date:
            raise ValueError("signal decision_at must fall on signal_date")
        return self


class PointInTimeStatistic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    trading_date: date
    available_at: AwareDatetime
    snapshot_hash: str = Field(min_length=64, max_length=64)
    value: Decimal = Field(ge=0)


class BenchmarkSeriesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    available_at: AwareDatetime
    snapshot_hash: str = Field(min_length=64, max_length=64)
    returns: dict[date, float]


class DailyNav(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trading_date: date
    cash: Decimal
    market_value: Decimal
    nav: Decimal


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    total_return: float
    annualized_return: float
    excess_return: float


class CapacityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scale_factor: Decimal = Field(gt=0)
    maximum_participation: Decimal = Field(ge=0)
    estimated_max_slippage_bps: Decimal = Field(ge=0)
    estimated_fill_rate: Decimal = Field(ge=0, le=1)
    feasible: bool


class CostAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commission: Decimal = Field(ge=0)
    stamp_tax: Decimal = Field(ge=0)
    transfer_fee: Decimal = Field(ge=0)
    slippage: Decimal = Field(ge=0)
    total_cost: Decimal = Field(ge=0)


class SymbolAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    industry_code: str
    gross_pnl: Decimal
    net_pnl: Decimal
    total_cost: Decimal = Field(ge=0)


class IndustryAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    industry_code: str
    gross_pnl: Decimal
    net_pnl: Decimal
    total_cost: Decimal = Field(ge=0)


class IndustryClassificationChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    old_industry_code: str
    new_industry_code: str
    changed_at: date
    attribution_method: str


class BacktestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_nav: tuple[DailyNav, ...]
    executions: tuple[ExecutionResult, ...]
    metrics: PerformanceMetrics
    benchmarks: tuple[BenchmarkResult, ...]
    capacity: tuple[CapacityResult, ...]
    symbol_attribution: tuple[SymbolAttribution, ...]
    industry_attribution: tuple[IndustryAttribution, ...]
    industry_classification_changes: tuple[IndustryClassificationChange, ...]
    attribution_method: str
    warnings: tuple[str, ...]
    cost_attribution: CostAttribution
    manifest_hash: str
    input_hash: str
    output_hash: str


class BacktestEngine:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.execution_model = DailyExecutionModel(config.execution)

    def run(
        self,
        *,
        trading_calendar: Sequence[date],
        bars: Mapping[tuple[date, str], ExecutionBar],
        rules: Mapping[tuple[date, str], TradingRule],
        signals: Sequence[BacktestSignal],
        adv_amounts: Mapping[tuple[date, str], PointInTimeStatistic],
        volatilities: Mapping[tuple[date, str], PointInTimeStatistic],
        benchmark_series: Sequence[BenchmarkSeriesInput],
    ) -> BacktestResult:
        calendar = tuple(sorted(set(trading_calendar)))
        if not calendar:
            raise ValueError("trading calendar cannot be empty")
        self._validate_inputs(
            calendar,
            bars,
            rules,
            signals,
            adv_amounts,
            volatilities,
            benchmark_series,
        )
        account = AccountState(cash=self.config.initial_cash)
        pending_orders: tuple[Order, ...] = ()
        daily_nav: list[DailyNav] = []
        executions: list[ExecutionResult] = []
        execution_dates: dict[str, date] = {}
        traded_notional = Decimal("0")
        signals_by_date: dict[date, list[BacktestSignal]] = defaultdict(list)
        for signal in signals:
            signals_by_date[signal.signal_date].append(signal)

        for index, trading_date in enumerate(calendar):
            day_bars = {
                symbol: bar for (bar_date, symbol), bar in bars.items() if bar_date == trading_date
            }
            day_rules = {
                symbol: rule
                for (rule_date, symbol), rule in rules.items()
                if rule_date == trading_date
            }
            if pending_orders:
                next_session = calendar[index + 1] if index + 1 < len(calendar) else date.max
                day_results = self.execution_model.execute_orders(
                    orders=pending_orders,
                    bars=day_bars,
                    rules=day_rules,
                    account=account,
                    next_trading_date=next_session,
                    adv_amounts={
                        symbol: adv_amounts[(trading_date, symbol)].value
                        for symbol in day_bars
                        if (trading_date, symbol) in adv_amounts
                    },
                    volatilities={
                        symbol: float(volatilities[(trading_date, symbol)].value)
                        for symbol in day_bars
                        if (trading_date, symbol) in volatilities
                    },
                )
                executions.extend(day_results)
                execution_dates.update({result.order_id: trading_date for result in day_results})
                traded_notional += sum((result.notional for result in day_results), Decimal("0"))
                pending_orders = ()

            market_value = self._market_value(account, day_bars)
            nav = account.cash + market_value
            daily_nav.append(
                DailyNav(
                    trading_date=trading_date,
                    cash=account.cash,
                    market_value=market_value,
                    nav=nav,
                )
            )

            if signals_by_date.get(trading_date) and index + 1 < len(calendar):
                pending_orders = self._plan_orders(
                    signals=signals_by_date[trading_date],
                    account=account,
                    nav=nav,
                    current_bars=day_bars,
                    next_rules={
                        symbol: rule
                        for (rule_date, symbol), rule in rules.items()
                        if rule_date == calendar[index + 1]
                    },
                )

        average_nav = sum((entry.nav for entry in daily_nav), Decimal("0")) / len(daily_nav)
        turnover = float(traded_notional / (Decimal("2") * average_nav)) if average_nav > 0 else 0.0
        metrics = calculate_performance_metrics(
            [float(entry.nav) for entry in daily_nav],
            annualization_sessions=self.config.annualization_sessions,
            annual_risk_free_rate=self.config.annual_risk_free_rate,
            turnover=turnover,
        )
        benchmarks = self._benchmark_results(calendar, benchmark_series, metrics.total_return)
        cost_attribution = self._cost_attribution(executions)
        (
            symbol_attribution,
            industry_attribution,
            industry_changes,
            warnings,
        ) = self._pnl_attribution(
            account,
            bars,
            calendar[-1],
            executions,
            execution_dates,
            signals,
        )
        attribution_method = "latest-research-industry-at-or-before-terminal-exposure-date"
        capacity = self._capacity(executions)
        input_hash = stable_hash(
            {
                "config": self.config,
                "calendar": calendar,
                "bars": self._sorted_models(bars),
                "rules": self._sorted_models(rules),
                "signals": [
                    signal.model_dump(mode="json")
                    for signal in sorted(
                        signals,
                        key=lambda item: (item.signal_date, item.symbol),
                    )
                ],
                "adv_amounts": self._sorted_models(adv_amounts),
                "volatilities": self._sorted_models(volatilities),
                "benchmarks": [
                    item.model_dump(mode="json")
                    for item in sorted(benchmark_series, key=lambda item: item.name)
                ],
            }
        )
        output_payload = {
            "daily_nav": [entry.model_dump(mode="json") for entry in daily_nav],
            "executions": [
                result.model_dump(mode="json")
                for result in sorted(executions, key=lambda item: item.order_id)
            ],
            "metrics": metrics.model_dump(mode="json"),
            "benchmarks": [item.model_dump(mode="json") for item in benchmarks],
            "capacity": [item.model_dump(mode="json") for item in capacity],
            "symbol_attribution": [item.model_dump(mode="json") for item in symbol_attribution],
            "industry_attribution": [item.model_dump(mode="json") for item in industry_attribution],
            "industry_classification_changes": [
                item.model_dump(mode="json") for item in industry_changes
            ],
            "attribution_method": attribution_method,
            "warnings": warnings,
            "cost_attribution": cost_attribution.model_dump(mode="json"),
            "manifest_hash": self.config.input_manifest.manifest_hash,
            "input_hash": input_hash,
        }
        return BacktestResult(
            daily_nav=tuple(daily_nav),
            executions=tuple(sorted(executions, key=lambda item: item.order_id)),
            metrics=metrics,
            benchmarks=benchmarks,
            capacity=capacity,
            symbol_attribution=symbol_attribution,
            industry_attribution=industry_attribution,
            industry_classification_changes=industry_changes,
            attribution_method=attribution_method,
            warnings=warnings,
            cost_attribution=cost_attribution,
            manifest_hash=self.config.input_manifest.manifest_hash,
            input_hash=input_hash,
            output_hash=stable_hash(output_payload),
        )

    def _validate_inputs(
        self,
        calendar: tuple[date, ...],
        bars: Mapping[tuple[date, str], ExecutionBar],
        rules: Mapping[tuple[date, str], TradingRule],
        signals: Sequence[BacktestSignal],
        adv_amounts: Mapping[tuple[date, str], PointInTimeStatistic],
        volatilities: Mapping[tuple[date, str], PointInTimeStatistic],
        benchmark_series: Sequence[BenchmarkSeriesInput],
    ) -> None:
        calendar_set = set(calendar)
        manifest = self.config.input_manifest
        allowed_hashes = set(manifest.snapshot_hashes)
        missing_adv = set(bars) - set(adv_amounts)
        missing_volatility = set(bars) - set(volatilities)
        missing_rules = set(bars) - set(rules)
        if missing_adv:
            raise ValueError(f"missing point-in-time ADV inputs: {sorted(missing_adv)}")
        if missing_volatility:
            raise ValueError(
                f"missing point-in-time volatility inputs: {sorted(missing_volatility)}"
            )
        if missing_rules:
            raise ValueError(f"missing frozen trading rules: {sorted(missing_rules)}")
        for (trading_date, symbol), bar in bars.items():
            if trading_date not in calendar_set:
                raise ValueError(f"bar outside trading calendar: {trading_date} {symbol}")
            if bar.trading_date != trading_date or bar.symbol != symbol:
                raise ValueError("bar key does not match bar payload")
            self._assert_frozen(bar.available_at, bar.snapshot_hash, allowed_hashes)

        for source_name, values in (
            ("ADV", adv_amounts),
            ("volatility", volatilities),
        ):
            for key, statistic in values.items():
                if key != (statistic.trading_date, statistic.symbol):
                    raise ValueError(f"{source_name} key does not match payload")
                if statistic.trading_date not in calendar_set:
                    raise ValueError(f"{source_name} outside trading calendar")
                if statistic.available_at.date() >= statistic.trading_date:
                    raise ValueError(f"{source_name} must be available before execution date")
                self._assert_frozen(
                    statistic.available_at,
                    statistic.snapshot_hash,
                    allowed_hashes,
                )

        for (trading_date, _), rule in rules.items():
            if trading_date not in calendar_set:
                raise ValueError("rule outside trading calendar")
            if not rule.enabled:
                raise ValueError("disabled trading rule supplied to backtest")
            if rule.available_at is None or rule.source_snapshot_hash is None:
                raise ValueError("backtest rules require frozen availability and snapshot hash")
            self._assert_frozen(rule.available_at, rule.source_snapshot_hash, allowed_hashes)
            if rule.published_at is not None and rule.published_at > rule.available_at:
                raise ValueError("rule cannot be ingested before it is published")

        seen: set[tuple[date, str]] = set()
        totals: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
        decisions: dict[date, AwareDatetime] = {}
        for signal in signals:
            key = (signal.signal_date, signal.symbol)
            if key in seen:
                raise ValueError(f"duplicate signal for {key}")
            seen.add(key)
            if signal.signal_date not in calendar_set:
                raise ValueError("signal date must be in the trading calendar")
            self._assert_frozen(signal.decision_at, signal.snapshot_hash, allowed_hashes)
            decision = decisions.setdefault(signal.signal_date, signal.decision_at)
            if decision != signal.decision_at:
                raise ValueError("signals on one date must share a decision_at")
            source_bar = bars.get(key)
            if source_bar is None or source_bar.available_at > signal.decision_at:
                raise ValueError("future bar data cannot feed a signal")
            totals[signal.signal_date] += signal.target_weight
        if any(total > 1 for total in totals.values()):
            raise ValueError("signal target weights exceed one")

        benchmark_by_name = {item.name: item for item in benchmark_series}
        if len(benchmark_by_name) != len(benchmark_series):
            raise ValueError("benchmark names must be unique")
        missing_benchmarks = set(self.config.required_benchmarks) - set(benchmark_by_name)
        if missing_benchmarks:
            raise ValueError(f"missing required benchmarks: {sorted(missing_benchmarks)}")
        for name in self.config.required_benchmarks:
            benchmark = benchmark_by_name[name]
            self._assert_frozen(
                benchmark.available_at,
                benchmark.snapshot_hash,
                allowed_hashes,
            )
            missing_dates = calendar_set - set(benchmark.returns)
            if missing_dates:
                raise ValueError(
                    f"benchmark {name} does not cover the full trading calendar: "
                    f"{sorted(missing_dates)}"
                )

    def _assert_frozen(
        self,
        available_at: AwareDatetime,
        snapshot_hash: str,
        allowed_hashes: set[str],
    ) -> None:
        if available_at > self.config.input_manifest.frozen_at:
            raise ValueError("input is newer than the frozen backtest manifest")
        if snapshot_hash not in allowed_hashes:
            raise ValueError("input snapshot is absent from the frozen backtest manifest")

    @staticmethod
    def _market_value(account: AccountState, bars: dict[str, ExecutionBar]) -> Decimal:
        total = Decimal("0")
        for lot in account.lots:
            bar = bars.get(lot.symbol)
            if bar is None:
                raise ValueError(f"missing valuation bar for held symbol {lot.symbol}")
            total += Decimal(lot.quantity) * bar.close
        return total

    @staticmethod
    def _plan_orders(
        *,
        signals: list[BacktestSignal],
        account: AccountState,
        nav: Decimal,
        current_bars: dict[str, ExecutionBar],
        next_rules: dict[str, TradingRule],
    ) -> tuple[Order, ...]:
        decision_at = signals[0].decision_at
        signal_date = signals[0].signal_date
        target_weights = {signal.symbol: signal.target_weight for signal in signals}
        symbols = set(target_weights) | {lot.symbol for lot in account.lots}
        orders: list[Order] = []
        for symbol in sorted(symbols):
            bar = current_bars.get(symbol)
            rule = next_rules.get(symbol)
            if bar is None or rule is None:
                raise ValueError(f"missing planning context for {symbol}")
            if rule.available_at is None or rule.available_at > decision_at:
                raise ValueError("future trading rule cannot be used to plan an order")
            if rule.published_at is not None and rule.published_at > decision_at:
                raise ValueError("unpublished trading rule cannot be used to plan an order")
            desired_value = nav * target_weights.get(symbol, Decimal("0"))
            lots = (desired_value / bar.close / rule.lot_size).to_integral_value(
                rounding=ROUND_FLOOR
            )
            desired_quantity = int(lots) * rule.lot_size
            current_quantity = account.total_quantity(symbol)
            difference = desired_quantity - current_quantity
            if difference == 0:
                continue
            side = Side.BUY if difference > 0 else Side.SELL
            orders.append(
                Order(
                    order_id=f"{signal_date.isoformat()}:{symbol}:{side.value}",
                    symbol=symbol,
                    side=side,
                    quantity=abs(difference),
                )
            )
        return tuple(orders)

    def _benchmark_results(
        self,
        calendar: tuple[date, ...],
        benchmark_series: Sequence[BenchmarkSeriesInput],
        strategy_total_return: float,
    ) -> tuple[BenchmarkResult, ...]:
        by_name = {item.name: item for item in benchmark_series}
        results: list[BenchmarkResult] = []
        for name in self.config.required_benchmarks:
            values = [by_name[name].returns[trading_date] for trading_date in calendar]
            total = float(self._compound(values))
            annualized = float(
                (1.0 + total) ** (self.config.annualization_sessions / len(values)) - 1.0
            )
            results.append(
                BenchmarkResult(
                    name=name,
                    total_return=total,
                    annualized_return=annualized,
                    excess_return=strategy_total_return - total,
                )
            )
        return tuple(results)

    def _capacity(self, executions: Sequence[ExecutionResult]) -> tuple[CapacityResult, ...]:
        traded = [result for result in executions if result.filled_quantity > 0]
        results: list[CapacityResult] = []
        for scale in self.config.capacity_scale_factors:
            maximum_participation = max(
                (result.participation_rate * scale for result in traded),
                default=Decimal("0"),
            )
            estimated_slippage = max(
                (result.slippage_bps * Decimal(str(sqrt(float(scale)))) for result in traded),
                default=Decimal("0"),
            )
            fill_rate = (
                min(
                    Decimal("1"),
                    self.config.capacity_max_participation / maximum_participation,
                )
                if maximum_participation > 0
                else Decimal("1")
            )
            feasible = (
                maximum_participation <= self.config.capacity_max_participation
                and estimated_slippage <= self.config.capacity_max_slippage_bps
                and fill_rate >= self.config.capacity_min_fill_rate
            )
            results.append(
                CapacityResult(
                    scale_factor=scale,
                    maximum_participation=maximum_participation,
                    estimated_max_slippage_bps=estimated_slippage,
                    estimated_fill_rate=fill_rate,
                    feasible=feasible,
                )
            )
        return tuple(results)

    @staticmethod
    def _cost_attribution(executions: Sequence[ExecutionResult]) -> CostAttribution:
        commission = sum((item.commission for item in executions), Decimal("0"))
        stamp_tax = sum((item.stamp_tax for item in executions), Decimal("0"))
        transfer_fee = sum((item.transfer_fee for item in executions), Decimal("0"))
        slippage = sum((item.slippage_cost for item in executions), Decimal("0"))
        return CostAttribution(
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage=slippage,
            total_cost=commission + stamp_tax + transfer_fee + slippage,
        )

    @staticmethod
    def _pnl_attribution(
        account: AccountState,
        bars: Mapping[tuple[date, str], ExecutionBar],
        final_date: date,
        executions: Sequence[ExecutionResult],
        execution_dates: Mapping[str, date],
        signals: Sequence[BacktestSignal],
    ) -> tuple[
        tuple[SymbolAttribution, ...],
        tuple[IndustryAttribution, ...],
        tuple[IndustryClassificationChange, ...],
        tuple[str, ...],
    ]:
        cash_flow: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        explicit_cost: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        slippage_cost: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for result in executions:
            direction = Decimal("-1") if result.side == Side.BUY else Decimal("1")
            cash_flow[result.symbol] += direction * result.notional
            explicit_cost[result.symbol] += result.total_fee
            slippage_cost[result.symbol] += result.slippage_cost
        ending_quantity: dict[str, int] = defaultdict(int)
        for lot in account.lots:
            ending_quantity[lot.symbol] += lot.quantity
        symbols = sorted(set(cash_flow) | set(ending_quantity))
        attribution_method = "latest-research-industry-at-or-before-terminal-exposure-date"
        signals_by_symbol: dict[str, list[BacktestSignal]] = defaultdict(list)
        for signal in sorted(signals, key=lambda item: (item.symbol, item.signal_date)):
            signals_by_symbol[signal.symbol].append(signal)
        industry_changes: list[IndustryClassificationChange] = []
        for symbol, symbol_signals in sorted(signals_by_symbol.items()):
            for previous, current in pairwise(symbol_signals):
                if previous.industry_code == current.industry_code:
                    continue
                industry_changes.append(
                    IndustryClassificationChange(
                        symbol=symbol,
                        old_industry_code=previous.industry_code,
                        new_industry_code=current.industry_code,
                        changed_at=current.signal_date,
                        attribution_method=attribution_method,
                    )
                )
        symbol_results: list[SymbolAttribution] = []
        industry_totals: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        for symbol in symbols:
            filled_dates = [
                execution_dates[result.order_id]
                for result in executions
                if result.symbol == symbol
                and result.filled_quantity > 0
                and result.order_id in execution_dates
            ]
            terminal_exposure_date = (
                final_date if ending_quantity[symbol] > 0 else max(filled_dates, default=final_date)
            )
            eligible_signals = [
                signal
                for signal in signals_by_symbol.get(symbol, ())
                if signal.signal_date <= terminal_exposure_date
            ]
            if not eligible_signals:
                raise ValueError(f"missing industry attribution for {symbol}")
            industry = max(
                eligible_signals, key=lambda item: (item.signal_date, item.decision_at)
            ).industry_code
            final_bar = bars.get((final_date, symbol))
            if final_bar is None and ending_quantity[symbol] > 0:
                raise ValueError(f"missing final attribution price for {symbol}")
            ending_value = (
                Decimal(ending_quantity[symbol]) * final_bar.close
                if final_bar is not None
                else Decimal("0")
            )
            net_pnl = ending_value + cash_flow[symbol] - explicit_cost[symbol]
            total_cost = explicit_cost[symbol] + slippage_cost[symbol]
            gross_pnl = net_pnl + total_cost
            symbol_results.append(
                SymbolAttribution(
                    symbol=symbol,
                    industry_code=industry,
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    total_cost=total_cost,
                )
            )
            old_gross, old_net, old_cost = industry_totals.get(
                industry,
                (Decimal("0"), Decimal("0"), Decimal("0")),
            )
            industry_totals[industry] = (
                old_gross + gross_pnl,
                old_net + net_pnl,
                old_cost + total_cost,
            )
        industry_results = tuple(
            IndustryAttribution(
                industry_code=industry,
                gross_pnl=values[0],
                net_pnl=values[1],
                total_cost=values[2],
            )
            for industry, values in sorted(industry_totals.items())
        )
        warnings = tuple(
            (
                f"industry classification changed for {item.symbol}: "
                f"{item.old_industry_code} -> {item.new_industry_code}; "
                f"attribution_method={item.attribution_method}"
            )
            for item in industry_changes
        )
        return tuple(symbol_results), industry_results, tuple(industry_changes), warnings

    @staticmethod
    def _sorted_models(
        values: Mapping[tuple[date, str], BaseModel],
    ) -> list[dict[str, object]]:
        return [
            values[key].model_dump(mode="json")
            for key in sorted(values, key=lambda item: (item[0], item[1]))
        ]

    @staticmethod
    def _compound(returns: list[float]) -> float:
        value = 1.0
        for daily_return in returns:
            value *= 1.0 + daily_return
        return value - 1.0
