from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_ai.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestSignal,
    BenchmarkSeriesInput,
    FrozenInputManifest,
    PointInTimeStatistic,
)
from ashare_ai.core.contracts import Side
from ashare_ai.trading.execution import (
    ExecutionBar,
    ExecutionConfig,
    OrderStatus,
    SlippageTier,
)
from ashare_ai.trading.rules import PriceLimitPolicy, TradingRule

SHANGHAI = ZoneInfo("Asia/Shanghai")
BAR_HASH = "a" * 64
RULE_HASH = "b" * 64
STAT_HASH = "c" * 64
SIGNAL_HASH = "d" * 64
BENCHMARK_HASH = "e" * 64
BENCHMARKS = ("CSI300", "CSI500", "EQUAL_WEIGHT_POOL")


def make_execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        participation_rate=Decimal("1"),
        price_limit_policy=PriceLimitPolicy(
            require_official_limits=True,
            default_price_tick=Decimal("0.01"),
        ),
        slippage_tiers=(
            SlippageTier(
                minimum_adv_amount=Decimal("0"),
                half_spread_bps=Decimal("0"),
            ),
        ),
        impact_coefficient=Decimal("0"),
        max_slippage_bps=Decimal("0"),
        fee_quantum=Decimal("0.01"),
        odd_lot_sell_all_only=True,
    )


def make_rule(day: date) -> TradingRule:
    visible_at = datetime(2025, 1, 1, 12, tzinfo=SHANGHAI)
    return TradingRule(
        rule_id=f"rule-{day}",
        rule_type="COMPOSITE",
        rule_version="v1",
        priority=1,
        exchange="SH",
        market="A",
        board="MAIN",
        security_type="STOCK",
        risk_status="NORMAL",
        is_st=False,
        effective_from=day,
        effective_to=None,
        published_at=visible_at,
        available_at=visible_at,
        source_snapshot_hash=RULE_HASH,
        price_limit_ratio=Decimal("0.10"),
        no_price_limit=False,
        lot_size=100,
        t_plus_one=True,
        stamp_tax_rate=Decimal("0"),
        commission_rate=Decimal("0"),
        minimum_commission=Decimal("0"),
        transfer_fee_rate=Decimal("0"),
        details={"price_tick": "0.01"},
    )


def make_bar(day: date, *, open_price: str, close_price: str, prev_close: str) -> ExecutionBar:
    opening = Decimal(open_price)
    closing = Decimal(close_price)
    previous = Decimal(prev_close)
    return ExecutionBar(
        symbol="600000.SH",
        trading_date=day,
        available_at=datetime(day.year, day.month, day.day, 17, tzinfo=SHANGHAI),
        snapshot_hash=BAR_HASH,
        open=opening,
        high=max(opening, closing),
        low=min(opening, closing),
        close=closing,
        volume=100_000,
        amount=Decimal("1000000"),
        prev_close=previous,
        official_limit_up=previous * Decimal("1.1"),
        official_limit_down=previous * Decimal("0.9"),
    )


def make_fixture() -> tuple[
    BacktestEngine,
    tuple[date, ...],
    dict[tuple[date, str], ExecutionBar],
    dict[tuple[date, str], TradingRule],
    dict[tuple[date, str], PointInTimeStatistic],
    dict[tuple[date, str], PointInTimeStatistic],
    tuple[BenchmarkSeriesInput, ...],
]:
    days = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))
    bars = {
        (days[0], "600000.SH"): make_bar(
            days[0], open_price="10", close_price="10", prev_close="10"
        ),
        (days[1], "600000.SH"): make_bar(
            days[1], open_price="10", close_price="11", prev_close="10"
        ),
        (days[2], "600000.SH"): make_bar(
            days[2], open_price="11", close_price="11", prev_close="11"
        ),
    }
    rules = {(day, "600000.SH"): make_rule(day) for day in days}
    adv = {
        (day, "600000.SH"): PointInTimeStatistic(
            symbol="600000.SH",
            trading_date=day,
            available_at=datetime.combine(
                day - timedelta(days=1),
                datetime.min.time().replace(hour=18),
                tzinfo=SHANGHAI,
            ),
            snapshot_hash=STAT_HASH,
            value=Decimal("100000000"),
        )
        for day in days
    }
    volatility = {
        key: value.model_copy(update={"value": Decimal("0")}) for key, value in adv.items()
    }
    frozen_at = datetime(2025, 1, 6, 20, tzinfo=SHANGHAI)
    benchmarks = tuple(
        BenchmarkSeriesInput(
            name=name,
            available_at=frozen_at,
            snapshot_hash=BENCHMARK_HASH,
            returns={days[0]: 0.0, days[1]: 0.01, days[2]: -0.01},
        )
        for name in BENCHMARKS
    )
    engine = BacktestEngine(
        BacktestConfig(
            initial_cash=Decimal("10000"),
            annualization_sessions=242,
            annual_risk_free_rate=0.0,
            execution=make_execution_config(),
            config_version="v1",
            input_manifest=FrozenInputManifest(
                manifest_id="fixture",
                frozen_at=frozen_at,
                snapshot_hashes=(
                    BAR_HASH,
                    RULE_HASH,
                    STAT_HASH,
                    SIGNAL_HASH,
                    BENCHMARK_HASH,
                ),
            ),
            required_benchmarks=BENCHMARKS,
            capacity_scale_factors=(Decimal("1"), Decimal("10")),
            capacity_max_participation=Decimal("0.05"),
            capacity_max_slippage_bps=Decimal("50"),
            capacity_min_fill_rate=Decimal("0.95"),
        )
    )
    return engine, days, bars, rules, adv, volatility, benchmarks


def make_signal(day: date, weight: str) -> BacktestSignal:
    return BacktestSignal(
        signal_date=day,
        decision_at=datetime(day.year, day.month, day.day, 18, tzinfo=SHANGHAI),
        snapshot_hash=SIGNAL_HASH,
        symbol="600000.SH",
        industry_code="BANK",
        target_weight=Decimal(weight),
    )


def make_industry_signal(day: date, weight: str, industry: str) -> BacktestSignal:
    return make_signal(day, weight).model_copy(update={"industry_code": industry})


def test_signal_is_executed_next_day_and_t1_sell_waits_until_following_session() -> None:
    engine, days, bars, rules, adv, volatility, benchmarks = make_fixture()
    result = engine.run(
        trading_calendar=days,
        bars=bars,
        rules=rules,
        signals=(make_signal(days[0], "0.5"), make_signal(days[1], "0")),
        adv_amounts=adv,
        volatilities=volatility,
        benchmark_series=benchmarks,
    )
    assert result.daily_nav[0].market_value == 0
    assert len(result.executions) == 2
    assert result.executions[0].side == Side.BUY
    assert result.executions[0].status == OrderStatus.FILLED
    assert result.executions[1].side == Side.SELL
    assert result.executions[1].status == OrderStatus.FILLED
    assert result.daily_nav[1].nav > result.daily_nav[0].nav
    assert tuple(item.name for item in result.benchmarks) == BENCHMARKS
    assert result.capacity
    assert result.symbol_attribution[0].symbol == "600000.SH"
    assert result.industry_attribution[0].industry_code == "BANK"
    assert result.cost_attribution.total_cost == 0
    assert result.output_hash == "b61c4528c3ed54123dd8e473118c557f8e84d64b49518e3fa6e54578bd54b29f"


def test_fixed_snapshot_hash_covers_adv_volatility_and_benchmarks() -> None:
    engine, days, bars, rules, adv, volatility, benchmarks = make_fixture()
    common = {
        "trading_calendar": tuple(reversed(days)),
        "bars": dict(reversed(list(bars.items()))),
        "rules": dict(reversed(list(rules.items()))),
        "signals": (make_signal(days[0], "0.5"),),
    }
    baseline = engine.run(
        **common,
        adv_amounts=adv,
        volatilities=volatility,
        benchmark_series=benchmarks,
    )
    repeated = engine.run(
        **common,
        adv_amounts=dict(reversed(list(adv.items()))),
        volatilities=dict(reversed(list(volatility.items()))),
        benchmark_series=tuple(reversed(benchmarks)),
    )
    assert baseline.input_hash == repeated.input_hash
    assert baseline.output_hash == repeated.output_hash

    adv_changed = dict(adv)
    first_key = sorted(adv_changed)[0]
    adv_changed[first_key] = adv_changed[first_key].model_copy(
        update={"value": Decimal("99999999")}
    )
    assert (
        engine.run(
            **common,
            adv_amounts=adv_changed,
            volatilities=volatility,
            benchmark_series=benchmarks,
        ).input_hash
        != baseline.input_hash
    )

    volatility_changed = dict(volatility)
    volatility_changed[first_key] = volatility_changed[first_key].model_copy(
        update={"value": Decimal("0.1")}
    )
    assert (
        engine.run(
            **common,
            adv_amounts=adv,
            volatilities=volatility_changed,
            benchmark_series=benchmarks,
        ).input_hash
        != baseline.input_hash
    )

    changed_benchmarks = list(benchmarks)
    changed_benchmarks[0] = changed_benchmarks[0].model_copy(
        update={"returns": {days[0]: 0.0, days[1]: 0.02, days[2]: -0.01}}
    )
    assert (
        engine.run(
            **common,
            adv_amounts=adv,
            volatilities=volatility,
            benchmark_series=changed_benchmarks,
        ).input_hash
        != baseline.input_hash
    )


def test_industry_changes_use_latest_signal_before_terminal_exposure() -> None:
    engine, days, bars, rules, adv, volatility, benchmarks = make_fixture()
    signals = (
        make_industry_signal(days[0], "0.5", "I0"),
        make_industry_signal(days[1], "0", "PLACEHOLDER_3"),
    )
    first = engine.run(
        trading_calendar=days,
        bars=bars,
        rules=rules,
        signals=signals,
        adv_amounts=adv,
        volatilities=volatility,
        benchmark_series=benchmarks,
    )
    second = engine.run(
        trading_calendar=tuple(reversed(days)),
        bars=dict(reversed(list(bars.items()))),
        rules=dict(reversed(list(rules.items()))),
        signals=tuple(reversed(signals)),
        adv_amounts=dict(reversed(list(adv.items()))),
        volatilities=dict(reversed(list(volatility.items()))),
        benchmark_series=tuple(reversed(benchmarks)),
    )
    assert first.symbol_attribution[0].industry_code == "PLACEHOLDER_3"
    assert first.industry_attribution[0].industry_code == "PLACEHOLDER_3"
    assert first.industry_classification_changes[0].old_industry_code == "I0"
    assert first.industry_classification_changes[0].new_industry_code == "PLACEHOLDER_3"
    assert first.attribution_method == (
        "latest-research-industry-at-or-before-terminal-exposure-date"
    )
    assert "I0 -> PLACEHOLDER_3" in first.warnings[0]
    assert first.output_hash == second.output_hash

def test_future_bar_canary_and_incomplete_benchmark_fail_closed() -> None:
    engine, days, bars, rules, adv, volatility, benchmarks = make_fixture()
    future_bars = dict(bars)
    future_bars[(days[0], "600000.SH")] = future_bars[(days[0], "600000.SH")].model_copy(
        update={"available_at": datetime(2025, 1, 2, 18, 0, 1, tzinfo=SHANGHAI)}
    )
    with pytest.raises(ValueError, match="future bar"):
        engine.run(
            trading_calendar=days,
            bars=future_bars,
            rules=rules,
            signals=(make_signal(days[0], "0.5"),),
            adv_amounts=adv,
            volatilities=volatility,
            benchmark_series=benchmarks,
        )

    incomplete = list(benchmarks)
    incomplete[0] = incomplete[0].model_copy(update={"returns": {days[0]: 0.0, days[1]: 0.01}})
    with pytest.raises(ValueError, match="full trading calendar"):
        engine.run(
            trading_calendar=days,
            bars=bars,
            rules=rules,
            signals=(make_signal(days[0], "0.5"),),
            adv_amounts=adv,
            volatilities=volatility,
            benchmark_series=incomplete,
        )
