from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_ai.core.contracts import Candidate
from ashare_ai.portfolio.builder import (
    CandidateQuote,
    PortfolioBuilder,
    PortfolioConfig,
    PortfolioFailureCode,
)
from ashare_ai.portfolio.events import (
    ActiveEventRisk,
    EventRiskPolicy,
    EventSeverity,
    aggregate_event_risk,
)
from ashare_ai.portfolio.risk import (
    DrawdownConfig,
    DrawdownControlState,
    PortfolioRiskState,
    evaluate_drawdown,
    transition_drawdown_state,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUOTE_HASH = "9" * 64


def make_quotes(count: int = 16) -> list[CandidateQuote]:
    quotes: list[CandidateQuote] = []
    for index in range(count):
        symbol = f"{600000 + index:06d}.SH"
        multiplier = 0.25 if index == 1 else 1.0
        quotes.append(
            CandidateQuote(
                candidate=Candidate(
                    symbol=symbol,
                    trading_date=date(2025, 1, 2),
                    decision_at=datetime(2025, 1, 2, 18, tzinfo=SHANGHAI),
                    total_score=100 - index,
                    prediction_percentile=1 - index / 100,
                    industry_code=f"I{index % 5}",
                    volatility=0.02,
                    event_risk_multiplier=multiplier,
                    style_exposures={"beta": 0.1, "size": 0.1},
                ),
                reference_price=Decimal("10"),
                available_at=datetime(2025, 1, 2, 17, tzinfo=SHANGHAI),
                snapshot_hash=QUOTE_HASH,
                lot_size=100,
            )
        )
    return quotes


def make_config() -> PortfolioConfig:
    return PortfolioConfig(
        target_count=15,
        maximum_single_weight=Decimal("0.08"),
        maximum_industry_weight=Decimal("0.25"),
        maximum_turnover=Decimal("0.20"),
        style_exposure_limits={"beta": Decimal("0.5"), "size": Decimal("0.5")},
        base_cash_weight=Decimal("0.02"),
        minimum_prediction_percentile=Decimal("0.50"),
        constraint_version="v1",
        enforce_turnover_on_initial=False,
        allocation_tolerance=Decimal("0.000000001"),
        maximum_allocation_iterations=100,
    )


def test_equal_risk_portfolio_obeys_name_industry_and_event_caps() -> None:
    result = PortfolioBuilder(make_config()).build(
        quotes=make_quotes(),
        nav=Decimal("10000000"),
        effective_trading_date=date(2025, 1, 3),
        current_weights={},
        risk_state=PortfolioRiskState.NORMAL,
        derisk_gross_multiplier=Decimal("0.5"),
    )
    assert result.success
    assert result.portfolio is not None
    assert len(result.portfolio.positions) == 15
    assert all(position.weight <= 0.08 + 1e-10 for position in result.portfolio.positions)
    industries: dict[str, float] = {}
    for position in result.portfolio.positions:
        industries[position.industry_code] = (
            industries.get(position.industry_code, 0.0) + position.weight
        )
    assert all(weight <= 0.25 + 1e-10 for weight in industries.values())
    weights = {position.symbol: position.weight for position in result.portfolio.positions}
    assert weights["600001.SH"] < weights["600002.SH"]


def test_turnover_violation_is_structured_failure() -> None:
    result = PortfolioBuilder(make_config()).build(
        quotes=make_quotes(),
        nav=Decimal("10000000"),
        effective_trading_date=date(2025, 1, 3),
        current_weights={"999999.SH": 0.98},
        risk_state=PortfolioRiskState.NORMAL,
        derisk_gross_multiplier=Decimal("0.5"),
    )
    assert not result.success
    assert result.failure is not None
    assert result.failure.code == PortfolioFailureCode.TURNOVER_INFEASIBLE


def test_builder_rejects_mixed_decision_dates_and_future_quotes() -> None:
    quotes = make_quotes()
    mixed = list(quotes)
    mixed[0] = mixed[0].model_copy(
        update={
            "candidate": mixed[0].candidate.model_copy(
                update={
                    "decision_at": datetime(2025, 1, 2, 19, tzinfo=SHANGHAI),
                }
            )
        }
    )
    result = PortfolioBuilder(make_config()).build(
        quotes=mixed,
        nav=Decimal("10000000"),
        effective_trading_date=date(2025, 1, 3),
        current_weights={},
        risk_state=PortfolioRiskState.NORMAL,
        derisk_gross_multiplier=Decimal("0.5"),
    )
    assert result.failure is not None
    assert result.failure.code == PortfolioFailureCode.MIXED_DECISION_AT

    future = list(quotes)
    future[0] = future[0].model_copy(
        update={"available_at": datetime(2025, 1, 2, 18, 0, 1, tzinfo=SHANGHAI)}
    )
    result = PortfolioBuilder(make_config()).build(
        quotes=future,
        nav=Decimal("10000000"),
        effective_trading_date=date(2025, 1, 3),
        current_weights={},
        risk_state=PortfolioRiskState.NORMAL,
        derisk_gross_multiplier=Decimal("0.5"),
    )
    assert result.failure is not None
    assert result.failure.code == PortfolioFailureCode.FUTURE_REFERENCE_PRICE


def test_style_exposure_violation_is_structured_failure() -> None:
    quotes = make_quotes()
    quotes = [
        quote.model_copy(
            update={
                "candidate": quote.candidate.model_copy(
                    update={"style_exposures": {"beta": 1.0, "size": 0.1}}
                )
            }
        )
        for quote in quotes
    ]
    result = PortfolioBuilder(make_config()).build(
        quotes=quotes,
        nav=Decimal("10000000"),
        effective_trading_date=date(2025, 1, 3),
        current_weights={},
        risk_state=PortfolioRiskState.NORMAL,
        derisk_gross_multiplier=Decimal("0.5"),
    )
    assert not result.success
    assert result.failure is not None
    assert result.failure.code == PortfolioFailureCode.STYLE_INFEASIBLE


def test_drawdown_fuse_prevents_new_portfolio() -> None:
    risk_config = DrawdownConfig(
        warning_threshold=Decimal("0.08"),
        fuse_threshold=Decimal("0.12"),
        derisk_gross_multiplier=Decimal("0.5"),
        minimum_observation_sessions=5,
        recovery_threshold=Decimal("0.08"),
    )
    state, drawdown = evaluate_drawdown(
        nav=Decimal("87"), high_watermark=Decimal("100"), config=risk_config
    )
    assert state == PortfolioRiskState.OBSERVE_ONLY
    assert drawdown == Decimal("0.13")
    result = PortfolioBuilder(make_config()).build(
        quotes=make_quotes(),
        nav=Decimal("10000000"),
        effective_trading_date=date(2025, 1, 3),
        current_weights={},
        risk_state=state,
        derisk_gross_multiplier=risk_config.derisk_gross_multiplier,
    )
    assert not result.success
    assert result.failure is not None
    assert result.failure.code == PortfolioFailureCode.FUSED


def test_drawdown_fuse_requires_observation_period_and_manual_recovery() -> None:
    config = DrawdownConfig(
        warning_threshold=Decimal("0.08"),
        fuse_threshold=Decimal("0.12"),
        derisk_gross_multiplier=Decimal("0.5"),
        minimum_observation_sessions=2,
        recovery_threshold=Decimal("0.08"),
    )
    state = DrawdownControlState(
        state=PortfolioRiskState.OBSERVE_ONLY,
        drawdown=Decimal("0.13"),
        observation_sessions=1,
    )
    still_fused = transition_drawdown_state(
        nav=Decimal("95"),
        high_watermark=Decimal("100"),
        config=config,
        previous=state,
        manual_recovery_confirmed=False,
    )
    assert still_fused.state == PortfolioRiskState.OBSERVE_ONLY
    recovered = transition_drawdown_state(
        nav=Decimal("95"),
        high_watermark=Decimal("100"),
        config=config,
        previous=still_fused,
        manual_recovery_confirmed=True,
    )
    assert recovered.state == PortfolioRiskState.NORMAL


def test_event_risk_uses_strictest_policy_and_trusted_block() -> None:
    policy = EventRiskPolicy(
        version="event-v1",
        multipliers={
            EventSeverity.LOW: 0.8,
            EventSeverity.MEDIUM: 0.5,
            EventSeverity.HIGH: 0.25,
            EventSeverity.CRITICAL: 0.0,
        },
        blocked_severities=frozenset({EventSeverity.CRITICAL}),
    )
    decision = aggregate_event_risk(
        [
            ActiveEventRisk(event_id="medium", severity=EventSeverity.MEDIUM, trusted_source=True),
            ActiveEventRisk(
                event_id="critical", severity=EventSeverity.CRITICAL, trusted_source=True
            ),
        ],
        policy,
    )
    assert decision.block_new
    assert decision.multiplier == 0
