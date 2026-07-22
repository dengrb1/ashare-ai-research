from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from math import sqrt
from pathlib import Path
from statistics import pstdev
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pyarrow.parquet as pq
from sqlalchemy.orm import Session

from ashare_ai.backtest.engine import (
    BacktestConfig,
    BacktestSignal,
    BenchmarkSeriesInput,
    FrozenInputManifest,
    PointInTimeStatistic,
)
from ashare_ai.core.contracts import TradeStatus
from ashare_ai.core.hashing import stable_hash
from ashare_ai.orchestration.akshare_bundle import BenchmarkDataNotReadyError
from ashare_ai.orchestration.builtin_backtest import (
    BacktestBundle,
    TradingRuleObservation,
    write_backtest_bundle,
)
from ashare_ai.orchestration.bundle import CanonicalDailyBundle
from ashare_ai.storage.models import JobRun, SnapshotManifestRow
from ashare_ai.trading.default_rules import ensure_builtin_trading_rules, listing_special_phase
from ashare_ai.trading.execution import ExecutionBar, ExecutionConfig, SlippageTier
from ashare_ai.trading.rules import PriceLimitPolicy, RuleContext, TradingRuleRepository


def create_backtest_snapshot(
    *,
    session: Session,
    run: JobRun,
    bundle: CanonicalDailyBundle,
    lake_root: str | Path,
    policy: Any,
    signals: tuple[BacktestSignal, ...] = (),
    phase: str = "INGEST_BASE",
    dataset: str = "backtest_bundle",
    extra_details: dict[str, Any] | None = None,
    prior_bundle: BacktestBundle | None = None,
    prior_snapshot_id: str | None = None,
) -> SnapshotManifestRow:
    ensure_builtin_trading_rules(session)
    logical_hash = stable_hash(
        {
            "run_id": run.run_id,
            "bundle": bundle,
            "rules": "effective-at-decision",
            "signals": signals,
            "phase": phase,
            "prior_snapshot_id": prior_snapshot_id,
        }
    )
    fixed_bundle = _build_bundle(session, run, bundle, logical_hash, policy, signals)
    if prior_bundle is not None:
        fixed_bundle = _merge_backtest_bundles(prior_bundle, fixed_bundle, run.run_id)
    target = Path(lake_root) / "backtest_bundle" / f"{run.run_id}-{phase.casefold()}.parquet"
    uri, file_hash = write_backtest_bundle(target, fixed_bundle)
    row_count = pq.ParquetFile(target).metadata.num_rows
    snapshot_id = str(uuid5(NAMESPACE_URL, f"backtest-bundle:{run.run_id}:{logical_hash}"))
    row = session.get(SnapshotManifestRow, snapshot_id)
    details = {
        "run_id": run.run_id,
        "user_id": run.user_id,
        "trading_date": run.trading_date.isoformat(),
        "parquet_file_sha256": file_hash,
        "bundle_payload_sha256": logical_hash,
        "research_price_basis": "RAW",
        "execution_price_basis": "RAW",
        "phase": phase,
        "signal_count": len(fixed_bundle.signals),
        "executable_signal_count": sum(
            item.signal_date < fixed_bundle.trading_calendar[-1]
            for item in fixed_bundle.signals
        ),
        "calendar_start": fixed_bundle.trading_calendar[0].isoformat(),
        "calendar_end": fixed_bundle.trading_calendar[-1].isoformat(),
        "future_trading_dates": [
            value.isoformat()
            for value in bundle.trading_calendar
            if value > bundle.trading_date
        ][:3],
        "calendar_source": bundle.calendar_source,
        "calendar_version": bundle.calendar_version,
        "data_quality": bundle.data_quality,
        "prior_snapshot_id": prior_snapshot_id,
        **(extra_details or {}),
    }
    if row is None:
        row = SnapshotManifestRow(
            snapshot_id=snapshot_id,
            run_id=run.run_id,
            dataset=dataset,
            source=_bundle_source(bundle),
            schema_version="1",
            adapter_version="builtin-backtest-bundle-v1",
            fetched_at=bundle.decision_at,
            row_count=row_count,
            payload_sha256=logical_hash,
            parquet_uri=uri,
            status="COMMITTED",
            details=details,
            committed_at=datetime.now(bundle.decision_at.tzinfo),
        )
        session.add(row)
    else:
        row.parquet_uri = uri
        row.row_count = row_count
        row.details = details
        row.status = "COMMITTED"
    session.flush()
    return row


def _build_bundle(
    session: Session,
    run: JobRun,
    bundle: CanonicalDailyBundle,
    logical_hash: str,
    policy: Any,
    signals: tuple[BacktestSignal, ...],
) -> BacktestBundle:
    by_symbol: dict[str, list[Any]] = defaultdict(list)
    for bar in sorted(bundle.bars, key=lambda item: (item.symbol, item.trading_date)):
        by_symbol[bar.symbol].append(bar)
    eligible_dates = sorted(
        {
            bar.trading_date
            for values in by_symbol.values()
            for index, bar in enumerate(values)
            if index >= 20
        }
    )
    if len(eligible_dates) < 20:
        raise RuntimeError("backtest snapshot requires at least 40 sessions of history")
    calendar = tuple(eligible_dates)
    calendar_set = set(calendar)
    securities = {item.symbol: item for item in bundle.securities}
    statuses: dict[str, list[Any]] = defaultdict(list)
    for item in bundle.statuses:
        statuses[item.symbol].append(item)
    rules: list[TradingRuleObservation] = []
    execution_bars: list[ExecutionBar] = []
    adv: list[PointInTimeStatistic] = []
    volatility: list[PointInTimeStatistic] = []
    rule_hashes: set[str] = set()
    repository = TradingRuleRepository()
    for symbol, values in by_symbol.items():
        security = securities[symbol]
        returns: list[Decimal] = []
        for index, bar in enumerate(values):
            if bar.prev_close:
                returns.append(bar.close / bar.prev_close - 1)
            if index < 20 or bar.trading_date not in calendar_set:
                continue
            previous = values[index - 20 : index]
            statistic_at = previous[-1].available_at
            adv_value = sum((item.amount for item in previous), Decimal("0")) / len(previous)
            recent_returns = returns[max(0, len(returns) - 20) :]
            volatility_value = (
                Decimal(str(pstdev(float(item) for item in recent_returns) * sqrt(242)))
                if len(recent_returns) > 1
                else Decimal("0")
            )
            visible_statuses = [
                item
                for item in statuses.get(symbol, ())
                if item.effective_from <= bar.trading_date
                and item.trading_date <= bar.trading_date
                and item.available_at <= bar.available_at
            ]
            status = max(
                visible_statuses,
                key=lambda item: (item.effective_from, item.available_at),
                default=None,
            )
            listing_days = max(0, (bar.trading_date - security.list_date).days)
            # A truncated history window must not be mistaken for the IPO window.
            # Only observations close to the actual effective list date receive a
            # finite listing-session number used by no-price-limit rules.
            listing_session = max(1, index + 1) if listing_days <= 14 else 10_000
            context = RuleContext(
                symbol=symbol,
                trading_date=bar.trading_date,
                decision_at=bar.available_at,
                exchange=security.exchange.value,
                market="A",
                board=security.board.value,
                security_type=security.security_type.value,
                risk_status="NORMAL",
                is_st=status.is_st if status is not None else False,
                listing_days=listing_days,
                listing_session=listing_session,
                special_phase=listing_special_phase(security.board.value, listing_session),
            )
            rule = repository.resolve(session, context)
            if rule.source_snapshot_hash is None or rule.available_at is None:
                raise RuntimeError(f"trading rule lacks frozen metadata: {rule.rule_id}")
            rule_hashes.add(rule.source_snapshot_hash)
            execution_bars.append(
                ExecutionBar(
                    symbol=symbol,
                    trading_date=bar.trading_date,
                    available_at=bar.available_at,
                    snapshot_hash=logical_hash,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=int(bar.volume),
                    amount=bar.amount,
                    prev_close=bar.prev_close,
                    trade_status=(
                        TradeStatus.SUSPENDED
                        if status is not None and status.is_suspended
                        else bar.trade_status
                    ),
                    price_basis="RAW",
                )
            )
            rules.append(
                TradingRuleObservation(
                    trading_date=bar.trading_date,
                    symbol=symbol,
                    rule=rule,
                )
            )
            adv.append(
                PointInTimeStatistic(
                    symbol=symbol,
                    trading_date=bar.trading_date,
                    available_at=statistic_at,
                    snapshot_hash=logical_hash,
                    value=adv_value,
                )
            )
            volatility.append(
                PointInTimeStatistic(
                    symbol=symbol,
                    trading_date=bar.trading_date,
                    available_at=statistic_at,
                    snapshot_hash=logical_hash,
                    value=max(Decimal("0"), volatility_value),
                )
            )
    benchmark_hash = stable_hash(
        {
            name: {value_date.isoformat(): value for value_date, value in values.items()}
            for name, values in bundle.benchmark_returns.items()
        }
    )
    required = tuple(policy.backtest.required_benchmarks)
    benchmark_values = bundle.benchmark_returns
    missing_dates_by_benchmark: dict[str, tuple[date, ...]] = {}
    for name in required:
        benchmark_return_by_date = benchmark_values.get(name, {})
        missing_dates = tuple(
            value_date for value_date in calendar if value_date not in benchmark_return_by_date
        )
        if missing_dates:
            missing_dates_by_benchmark[name] = missing_dates
    if missing_dates_by_benchmark:
        raise BenchmarkDataNotReadyError(
            target_date=bundle.trading_date,
            missing_benchmarks=tuple(missing_dates_by_benchmark),
            last_available_dates={
                name: max(
                    (
                        value_date
                        for value_date in benchmark_values.get(name, {})
                        if value_date <= bundle.trading_date
                    ),
                    default=None,
                )
                for name in missing_dates_by_benchmark
            },
            missing_dates_by_benchmark=missing_dates_by_benchmark,
        )
    benchmarks = tuple(
        BenchmarkSeriesInput(
            name=name,
            available_at=bundle.decision_at,
            snapshot_hash=benchmark_hash,
            returns={trading_date: values[trading_date] for trading_date in calendar},
        )
        for name, values in sorted(benchmark_values.items())
        if set(calendar) <= set(values)
    )
    hashes = tuple(
        sorted(
            {
                logical_hash,
                benchmark_hash,
                *rule_hashes,
                *(item.snapshot_hash for item in signals),
            }
        )
    )
    config = BacktestConfig(
        initial_cash=Decimal("10000000"),
        annualization_sessions=242,
        annual_risk_free_rate=0.0,
        execution=ExecutionConfig(
            participation_rate=policy.execution.participation_rate,
            price_limit_policy=PriceLimitPolicy(
                require_official_limits=False,
                default_price_tick=Decimal("0.01"),
            ),
            slippage_tiers=(
                SlippageTier(
                    minimum_adv_amount=Decimal("0"),
                    half_spread_bps=Decimal("5"),
                ),
            ),
            impact_coefficient=policy.execution.impact_coefficient,
            max_slippage_bps=policy.execution.maximum_slippage_bps,
            fee_quantum=Decimal("0.01"),
            odd_lot_sell_all_only=True,
        ),
        config_version=f"{policy.version}-snapshot-v1",
        input_manifest=FrozenInputManifest(
            manifest_id=f"research-run:{run.run_id}",
            frozen_at=bundle.decision_at,
            snapshot_hashes=hashes,
        ),
        required_benchmarks=required,
        capacity_scale_factors=(Decimal("1"), Decimal("10")),
        capacity_max_participation=policy.backtest.capacity_max_participation,
        capacity_max_slippage_bps=policy.backtest.capacity_max_impact_bps,
        capacity_min_fill_rate=policy.backtest.capacity_min_fill_rate,
    )
    return BacktestBundle(
        config=config,
        trading_calendar=calendar,
        bars=tuple(execution_bars),
        rules=tuple(rules),
        signals=signals,
        adv_amounts=tuple(adv),
        volatilities=tuple(volatility),
        benchmarks=benchmarks,
    )


def _merge_backtest_bundles(
    prior: BacktestBundle,
    current: BacktestBundle,
    run_id: str,
) -> BacktestBundle:
    def merge_by_key(
        prior_items: tuple[Any, ...],
        current_items: tuple[Any, ...],
        key: Callable[[Any], Any],
    ) -> tuple[Any, ...]:
        merged = {key(item): item for item in current_items}
        # Previously frozen observations win on overlap so later HFQ revisions cannot
        # rewrite an already committed historical backtest input.
        merged.update({key(item): item for item in prior_items})
        return tuple(merged[value] for value in sorted(merged))

    benchmarks = []
    current_benchmarks = {item.name: item for item in current.benchmarks}
    prior_benchmarks = {item.name: item for item in prior.benchmarks}
    for name in sorted(set(current_benchmarks) | set(prior_benchmarks)):
        newer = current_benchmarks.get(name)
        older = prior_benchmarks.get(name)
        if newer is None:
            benchmarks.append(older)
            continue
        if older is None:
            benchmarks.append(newer)
            continue
        returns = {**newer.returns, **older.returns}
        benchmarks.append(
            newer.model_copy(
                update={
                    "returns": returns,
                    "snapshot_hash": stable_hash(
                        {value_date.isoformat(): value for value_date, value in returns.items()}
                    ),
                }
            )
        )
    manifest = current.config.input_manifest.model_copy(
        update={
            "manifest_id": f"research-run:{run_id}:cumulative",
            "snapshot_hashes": tuple(
                sorted(
                    set(prior.config.input_manifest.snapshot_hashes)
                    | set(current.config.input_manifest.snapshot_hashes)
                    | {item.snapshot_hash for item in benchmarks if item is not None}
                )
            ),
        }
    )
    return BacktestBundle(
        config=current.config.model_copy(update={"input_manifest": manifest}),
        trading_calendar=tuple(
            sorted(set(prior.trading_calendar) | set(current.trading_calendar))
        ),
        bars=merge_by_key(
            prior.bars, current.bars, lambda item: (item.trading_date, item.symbol)
        ),
        rules=merge_by_key(
            prior.rules, current.rules, lambda item: (item.trading_date, item.symbol)
        ),
        signals=merge_by_key(
            prior.signals, current.signals, lambda item: (item.signal_date, item.symbol)
        ),
        adv_amounts=merge_by_key(
            prior.adv_amounts,
            current.adv_amounts,
            lambda item: (item.trading_date, item.symbol),
        ),
        volatilities=merge_by_key(
            prior.volatilities,
            current.volatilities,
            lambda item: (item.trading_date, item.symbol),
        ),
        benchmarks=tuple(item for item in benchmarks if item is not None),
    )


def _bundle_source(bundle: CanonicalDailyBundle) -> str:
    sources = {item.source for item in bundle.securities}
    return next(iter(sources)) if len(sources) == 1 else "mixed"
