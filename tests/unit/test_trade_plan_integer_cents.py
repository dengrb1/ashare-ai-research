from __future__ import annotations

from decimal import Decimal

from test_trade_plan_optimizer import _config, _history, _rule

from ashare_ai.backtest.trade_plan import optimize_trade_strategy
from ashare_ai.core.hashing import stable_hash


def test_full_strategy_grid_keeps_pre_integer_cents_golden_hash() -> None:
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

    # Covers the default 3 x 3 x 3 x 2 x 4 optimizer grid.
    assert stable_hash(strategy) == (
        "3d6c20b192bc6e9835c32236ade2fe181f71c206b29256c4e1922bbe0d70a5cd"
    )
