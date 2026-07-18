from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from ashare_ai.backtest.trade_plan import (
    StrategyParameters,
    TradePlanCandidateInput,
    TradePlanOutcome,
    TradePlanPositionInput,
    _simulate_one_entry,
    build_deterministic_trade_plan,
    optimize_trade_strategy,
)
from ashare_ai.core.time import SHANGHAI
from ashare_ai.trading.execution import (
    DailyExecutionModel,
    ExecutionBar,
    ExecutionConfig,
    SlippageTier,
)
from ashare_ai.trading.rules import PriceLimitPolicy, TradingRule

HASH = "a" * 64


def _config() -> ExecutionConfig:
    return ExecutionConfig(
        participation_rate=Decimal("1"),
        price_limit_policy=PriceLimitPolicy(
            require_official_limits=True,
            default_price_tick=Decimal("0.01"),
        ),
        slippage_tiers=(
            SlippageTier(minimum_adv_amount=Decimal("0"), half_spread_bps=Decimal("0")),
        ),
        impact_coefficient=Decimal("0"),
        max_slippage_bps=Decimal("0"),
        fee_quantum=Decimal("0.01"),
        odd_lot_sell_all_only=True,
    )


def _rule(day: date) -> TradingRule:
    visible = datetime(2025, 1, 1, 12, tzinfo=SHANGHAI)
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
        published_at=visible,
        available_at=visible,
        source_snapshot_hash="b" * 64,
        price_limit_ratio=Decimal("0.10"),
        no_price_limit=False,
        lot_size=100,
        t_plus_one=True,
        stamp_tax_rate=Decimal("0.001"),
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5"),
        transfer_fee_rate=Decimal("0.00001"),
        details={"price_tick": "0.01", "buy_qty_step": 100, "sell_qty_step": 100},
    )


def _history(count: int) -> tuple[tuple[date, ...], tuple[ExecutionBar, ...]]:
    days: list[date] = []
    current = date(2025, 1, 2)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    bars = []
    previous = Decimal("10")
    for day in days:
        opening = previous
        close = opening * Decimal("1.006")
        bars.append(
            ExecutionBar(
                symbol="600000.SH",
                trading_date=day,
                available_at=datetime(day.year, day.month, day.day, 18, tzinfo=SHANGHAI),
                snapshot_hash=HASH,
                open=opening,
                high=close * Decimal("1.01"),
                low=opening * Decimal("0.99"),
                close=close,
                volume=10_000_000,
                amount=Decimal("100000000"),
                prev_close=previous,
                official_limit_up=previous * Decimal("1.10"),
                official_limit_down=previous * Decimal("0.90"),
                price_basis="RAW",
            )
        )
        previous = close
    return tuple(days), tuple(bars)


def test_optimizer_rejects_claim_when_history_is_below_120_sessions() -> None:
    calendar, bars = _history(119)
    result = optimize_trade_strategy(
        symbol="600000.SH",
        trading_calendar=calendar,
        bars=bars,
        rules={day: _rule(day) for day in calendar},
        adv_amounts={day: Decimal("100000000") for day in calendar},
        volatilities={day: 0.2 for day in calendar},
        execution_config=_config(),
    )
    assert result.outcome == TradePlanOutcome.NO_BUY
    assert result.reason_code == "INSUFFICIENT_HISTORY"


def test_optimizer_selects_only_positive_oos_strategy_with_required_trades() -> None:
    calendar, bars = _history(240)
    result = optimize_trade_strategy(
        symbol="600000.SH",
        trading_calendar=calendar,
        bars=bars,
        rules={day: _rule(day) for day in calendar},
        adv_amounts={day: Decimal("100000000") for day in calendar},
        volatilities={day: 0.2 for day in calendar},
        execution_config=_config(),
    )
    assert result.outcome == TradePlanOutcome.BUY
    assert result.training_sessions == 160
    assert result.validation_sessions == 80
    assert result.validation_metrics is not None
    assert result.validation_metrics.net_return > 0
    assert result.validation_metrics.completed_trades >= 5
    assert result.validation_metrics.maximum_drawdown <= 0.12


def test_plan_quantity_respects_existing_position_lot_and_caps() -> None:
    calendar, bars = _history(240)
    strategy = optimize_trade_strategy(
        symbol="600000.SH",
        trading_calendar=calendar,
        bars=bars,
        rules={day: _rule(day) for day in calendar},
        adv_amounts={day: Decimal("100000000") for day in calendar},
        volatilities={day: 0.2 for day in calendar},
        execution_config=_config(),
    )
    reference = bars[-1].close
    plan = build_deterministic_trade_plan(
        trading_date=calendar[-1],
        decision_at=datetime(2026, 1, 1, 18, tzinfo=SHANGHAI).isoformat(),
        candidates=(
            TradePlanCandidateInput(
                symbol="600000.SH",
                industry_code="BANK",
                volatility=0.2,
                event_risk_multiplier=1,
            ),
        ),
        positions=(TradePlanPositionInput(symbol="600000.SH", quantity=100),),
        strategies={"600000.SH": strategy},
        reference_prices={"600000.SH": reference},
        effective_rules={"600000.SH": _rule(calendar[-1])},
        future_trading_dates=(date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)),
        total_assets=Decimal("1000000"),
        available_cash=Decimal("500000"),
        requested_budget=Decimal("500000"),
    )
    decision = plan.symbol_plans[0]
    assert decision.suggested_additional_quantity % 100 == 0
    assert decision.target_weight <= 0.08 + 0.001
    assert decision.target_total_quantity == 100 + decision.suggested_additional_quantity
    assert decision.earliest_sell_date == date(2026, 1, 6)


def test_same_day_take_profit_and_stop_loss_uses_conservative_stop() -> None:
    calendar, base_bars = _history(3)
    exit_bar = base_bars[2].model_copy(
        update={
            "open": Decimal("10.10"),
            "high": Decimal("12"),
            "low": Decimal("8"),
            "close": Decimal("10"),
        }
    )
    bars = {item.trading_date: item for item in (*base_bars[:2], exit_bar)}
    outcome = _simulate_one_entry(
        symbol="600000.SH",
        calendar=calendar,
        entry_index=0,
        bars=bars,
        rules={day: _rule(day) for day in calendar},
        adv_amounts={day: Decimal("100000000") for day in calendar},
        volatilities={day: 0 for day in calendar},
        execution=DailyExecutionModel(_config()),
        parameters=StrategyParameters(
            entry_discount=Decimal("0"),
            take_profit=Decimal("0.08"),
            stop_loss=Decimal("0.05"),
            trailing_stop=Decimal("0.08"),
            max_holding_sessions=10,
            entry_valid_sessions=3,
        ),
    )

    assert outcome is not None
    assert outcome < 0
