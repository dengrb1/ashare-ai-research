"""Decision provider for the parameterizable factor scoring formula.

The provider turns a frozen, point-in-time :class:`MarketState` (optionally
with the :class:`CompositeScore` the research pipeline already computed) into a
``UnifiedDecision`` using the deterministic arithmetic in
:mod:`ashare_ai.scoring.factor`.

It never loads a Torch model, never calls a remote endpoint, and never writes
state.  Given the same inputs and the same genome it returns byte-identical
output.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ashare_ai.agents.decision.models import (
    ActionProbabilities,
    BinaryProbabilities,
    DecisionProbabilities,
    DirectionProbabilities,
    MarketState,
    RiskProbabilities,
    UnifiedDecision,
)
from ashare_ai.agents.decision.protocols import DecisionProvider
from ashare_ai.core.hashing import stable_hash
from ashare_ai.scoring.factor import (
    DEFAULT_FACTOR_GENOME,
    SHADOW_FACTOR_GENOME,
    SHADOW_FACTOR_FORMULA_VERSION,
    calculate_factor_score,
    factor_action,
    factor_position_level,
    factor_risk,
    factor_tilt,
)

if TYPE_CHECKING:
    from ashare_ai.core.contracts import CompositeScore
    from ashare_ai.scoring.factor import FactorGenome

logger = logging.getLogger(__name__)

# Neutral value used whenever a component has no point-in-time evidence.  A
# missing input must never be silently read as bullish or bearish.
NEUTRAL_COMPONENT_SCORE = 50.0


def _clamp_score(value: float) -> float:
    return max(0.0, min(100.0, value))


def _technical_component(state: MarketState) -> float:
    """Deterministic technical sub-score from frozen indicators only."""

    signals: list[float] = []
    if state.ma20 and state.close:
        signals.append(_clamp_score(50.0 + (state.close - state.ma20) / state.ma20 * 500.0))
    if state.macd_hist is not None and state.close:
        signals.append(_clamp_score(50.0 + state.macd_hist / state.close * 2000.0))
    if state.rsi is not None:
        # RSI 50 is neutral; 30/70 are the conventional oversold/overbought ends.
        signals.append(_clamp_score(50.0 + (state.rsi - 50.0) * 1.0))
    if not signals:
        return NEUTRAL_COMPONENT_SCORE
    return round(sum(signals) / len(signals), 6)


def _fundamental_component(state: MarketState) -> float:
    """Deterministic fundamental sub-score from frozen valuation fields only."""

    signals: list[float] = []
    if state.roe is not None:
        signals.append(_clamp_score(50.0 + state.roe * 200.0))
    if state.revenue_growth_yoy is not None:
        signals.append(_clamp_score(50.0 + state.revenue_growth_yoy * 100.0))
    if state.net_profit_growth_yoy is not None:
        signals.append(_clamp_score(50.0 + state.net_profit_growth_yoy * 100.0))
    if state.pe_ttm is not None:
        # Cheaper than 25x TTM is treated as supportive, richer as a headwind.
        signals.append(_clamp_score(50.0 + (25.0 - state.pe_ttm) * 1.0))
    if not signals:
        return NEUTRAL_COMPONENT_SCORE
    return round(sum(signals) / len(signals), 6)


def _sentiment_component(state: MarketState) -> float:
    """Deterministic sentiment sub-score from the frozen sentiment field only."""

    if state.sentiment_score is not None:
        return round(_clamp_score(state.sentiment_score), 6)
    return NEUTRAL_COMPONENT_SCORE


def _quality_component(state: MarketState) -> float:
    """Evidence completeness of the frozen state, as a 0-100 confidence score."""

    tracked = (
        state.ma5,
        state.ma10,
        state.ma20,
        state.ma60,
        state.macd_hist,
        state.rsi,
        state.kdj_k,
        state.bollinger_middle,
        state.turnover_rate,
        state.pe_ttm,
        state.pb,
        state.roe,
        state.revenue_growth_yoy,
        state.net_profit_growth_yoy,
        state.sentiment_score,
        state.news_count_7d,
        state.industry_code,
    )
    present = sum(1 for value in tracked if value is not None)
    return round(100.0 * present / len(tracked), 6)


def _shadow_components(state: MarketState) -> dict[str, float]:
    """Map frozen technical fields to bounded v2 shadow components."""

    momentum = 50.0
    if state.return_5d is not None:
        momentum = _clamp_score(50.0 + state.return_5d * 500.0)
    elif state.index_return_5d is not None:
        momentum = _clamp_score(50.0 + state.index_return_5d * 500.0)
    elif state.ma20 and state.close:
        momentum = _clamp_score(50.0 + (state.close / state.ma20 - 1.0) * 500.0)

    volatility = 50.0
    if state.annualized_volatility_20d is not None:
        volatility = _clamp_score(85.0 - state.annualized_volatility_20d * 100.0)
    elif state.max_drawdown_60d is not None:
        volatility = _clamp_score(80.0 - state.max_drawdown_60d * 100.0)
    elif state.bollinger_upper and state.bollinger_lower and state.bollinger_upper > state.bollinger_lower:
        spread = (state.bollinger_upper - state.bollinger_lower) / max(state.close, 1e-9)
        volatility = _clamp_score(80.0 - spread * 500.0)
    elif state.rsi is not None:
        volatility = _clamp_score(80.0 - abs(state.rsi - 50.0) * 0.6)

    liquidity = 50.0
    if state.volume_ratio_5_to_20 is not None:
        liquidity = _clamp_score(50.0 + (state.volume_ratio_5_to_20 - 1.0) * 35.0)
    elif state.turnover_rate is not None:
        liquidity = _clamp_score(50.0 + (state.turnover_rate - 0.02) * 1000.0)
    elif state.amount > 0 and state.volume > 0:
        liquidity = _clamp_score(50.0 + min(0.05, state.amount / max(state.volume, 1.0) / max(state.close, 1.0) / 100.0) * 1000.0)
    return {"momentum": round(momentum, 6), "volatility": round(volatility, 6), "liquidity": round(liquidity, 6)}


def _action_probabilities(
    score: float, genome: FactorGenome
) -> tuple[str, ActionProbabilities]:
    """Deterministic, normalized action distribution around the HOLD band."""

    tilt = factor_tilt(score, genome)
    buy = 0.25 + 0.45 * max(tilt, 0.0)
    sell = 0.25 + 0.45 * max(-tilt, 0.0)
    hold = max(0.02, 1.0 - buy - sell)
    total = buy + hold + sell
    probabilities = ActionProbabilities(BUY=buy / total, HOLD=hold / total, SELL=sell / total)
    action = factor_action(score, genome)
    return action, probabilities


def _risk_probabilities(
    score: float, genome: FactorGenome
) -> tuple[str, RiskProbabilities]:
    """Deterministic, normalized risk distribution around the risk bands."""

    span = max(1e-6, genome.risk_low_threshold - genome.risk_high_threshold)
    midpoint = (genome.risk_low_threshold + genome.risk_high_threshold) / 2.0
    tilt = max(-1.0, min(1.0, (score - midpoint) / span))
    low = 0.20 + 0.50 * max(tilt, 0.0)
    high = 0.20 + 0.50 * max(-tilt, 0.0)
    medium = max(0.02, 1.0 - low - high)
    total = low + medium + high
    probabilities = RiskProbabilities(
        LOW=low / total, MEDIUM=medium / total, HIGH=high / total
    )
    risk = factor_risk(score, genome)
    return risk, probabilities


def _direction_probabilities(
    score: float, genome: FactorGenome
) -> tuple[DirectionProbabilities, DirectionProbabilities, BinaryProbabilities]:
    """Deterministic direction distributions derived from the score tilt."""

    tilt = factor_tilt(score, genome)
    up_1d = 0.34 + 0.28 * max(tilt, 0.0)
    down_1d = 0.34 + 0.28 * max(-tilt, 0.0)
    flat_1d = max(0.02, 1.0 - up_1d - down_1d)
    total_1d = up_1d + flat_1d + down_1d

    up_5d = 0.36 + 0.30 * max(tilt, 0.0)
    down_5d = 0.36 + 0.30 * max(-tilt, 0.0)
    flat_5d = max(0.02, 1.0 - up_5d - down_5d)
    total_5d = up_5d + flat_5d + down_5d

    yes = max(0.05, min(0.95, 0.40 + 0.30 * tilt))
    return (
        DirectionProbabilities(
            UP=up_1d / total_1d, FLAT=flat_1d / total_1d, DOWN=down_1d / total_1d
        ),
        DirectionProbabilities(
            UP=up_5d / total_5d, FLAT=flat_5d / total_5d, DOWN=down_5d / total_5d
        ),
        BinaryProbabilities(YES=yes, NO=1.0 - yes),
    )


class FactorDecisionProvider(DecisionProvider):
    """Deterministic factor provider; the only provider that creates decisions."""

    def __init__(self, genome: FactorGenome | None = None):
        self._genome = genome or DEFAULT_FACTOR_GENOME
        self._model_version = self._genome.formula_version

    @property
    def mode(self) -> str:
        return "factor"

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def genome(self) -> FactorGenome:
        return self._genome

    async def predict(
        self,
        market_state: MarketState,
        composite_score: CompositeScore | None = None,
    ) -> UnifiedDecision:
        """Score one frozen market state.

        When the research pipeline already produced a ``CompositeScore`` its
        component scores, dividend bonus, event-risk multiplier and frozen
        market-index snapshot are reused verbatim.  Otherwise the components are
        derived deterministically from the frozen ``MarketState`` fields, with
        missing evidence scored neutrally.
        """

        if composite_score is not None:
            if composite_score.symbol != market_state.symbol:
                raise ValueError("composite score symbol does not match market state")
            if composite_score.trading_date != market_state.trading_date:
                raise ValueError("composite score trading_date does not match market state")
            if composite_score.decision_at.date() != market_state.trading_date:
                raise ValueError("composite score decision_at must fall on trading_date")
            fundamental = composite_score.fundamental_score
            technical = composite_score.technical_score
            sentiment = composite_score.sentiment_score
            quality = composite_score.quality_confidence_score
            dividend_bonus = composite_score.dividend_bonus
            event_risk_multiplier = composite_score.event_risk_multiplier
            market_adjustment = composite_score.market_score_adjustment
            market_multiplier = composite_score.market_risk_multiplier
            input_manifest_hash = composite_score.agent_bundle_sha256
            dataset_hash = stable_hash(
                {
                    "feature_snapshot_id": str(composite_score.feature_snapshot_id),
                    "evidence_bundle_sha256": composite_score.evidence_bundle_sha256,
                    "trading_date": composite_score.trading_date.isoformat(),
                }
            )
        else:
            fundamental = _fundamental_component(market_state)
            technical = _technical_component(market_state)
            sentiment = _sentiment_component(market_state)
            quality = _quality_component(market_state)
            dividend_bonus = 0.0
            event_risk_multiplier = 1.0
            market_adjustment = 0.0
            market_multiplier = 1.0
            input_manifest_hash = stable_hash(market_state.model_dump(mode="json"))
            dataset_hash = stable_hash(
                {
                    "symbol": market_state.symbol,
                    "trading_date": market_state.trading_date.isoformat(),
                }
            )

        score = calculate_factor_score(
            fundamental_score=fundamental,
            technical_score=technical,
            sentiment_score=sentiment,
            quality_confidence_score=quality,
            genome=self._genome,
            dividend_bonus=dividend_bonus,
            event_risk_multiplier=event_risk_multiplier,
            market_score_adjustment=market_adjustment,
            market_risk_multiplier=market_multiplier,
        )
        shadow = _shadow_components(market_state)
        shadow_score = calculate_factor_score(
            fundamental_score=fundamental,
            technical_score=technical,
            sentiment_score=sentiment,
            quality_confidence_score=quality,
            genome=SHADOW_FACTOR_GENOME,
            dividend_bonus=dividend_bonus,
            event_risk_multiplier=event_risk_multiplier,
            market_score_adjustment=market_adjustment,
            market_risk_multiplier=market_multiplier,
            **{f"{name}_score": value for name, value in shadow.items()},
        )

        action, action_probabilities = _action_probabilities(score, self._genome)
        risk, risk_probabilities = _risk_probabilities(score, self._genome)
        direction_1d, direction_5d, up_over_3pct = _direction_probabilities(score, self._genome)

        return UnifiedDecision(
            symbol=market_state.symbol,
            trading_date=market_state.trading_date,
            available_at=market_state.available_at,
            decision_at=(
                composite_score.decision_at
                if composite_score is not None
                else market_state.available_at
            ),
            probabilities=DecisionProbabilities(
                direction_1d=direction_1d,
                direction_5d=direction_5d,
                up_over_3pct_5d=up_over_3pct,
                action=action_probabilities,
                risk=risk_probabilities,
            ),
            action=action,
            risk=risk,
            position=factor_position_level(score, self._genome),
            mode="factor",
            model_version=self._model_version,
            input_manifest_hash=input_manifest_hash,
            formula_version=self._genome.formula_version,
            parameter_sha256=self._genome.parameter_sha256,
            dataset_sha256=dataset_hash,
            shadow_formula_version=SHADOW_FACTOR_FORMULA_VERSION,
            shadow_score=shadow_score,
            shadow_factor_evidence=shadow,
        )
