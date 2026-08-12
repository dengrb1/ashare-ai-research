from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from pydantic import AwareDatetime

from ashare_ai.core.contracts import (
    AgentComponentResult,
    CompositeScore,
    DataQualityInputs,
    MarketIndexPerformance,
    MarketIndexSnapshot,
)
from ashare_ai.core.hashing import stable_hash

FORMULA_VERSION = "composite-35-35-20-10-v1"
FORMULA_VERSION_V2 = "composite-35-35-20-10-dividend-news-v2"
FORMULA_VERSION_V3 = "composite-35-35-20-10-dividend-news-market-v3"
QUALITY_VERSION = "quality-v1"

_MARKET_INDEX_SYMBOLS = {
    "CSI300": "000300.SH",
    "CSI500": "000905.SH",
    "CSI1000": "000852.SH",
}
_MARKET_INDEX_WEIGHTS = {"CSI300": 0.5, "CSI500": 0.3, "CSI1000": 0.2}

_COMPONENT_WEIGHTS = {
    "fundamental": 0.35,
    "technical": 0.35,
    "sentiment": 0.20,
}
_QUALITY_WEIGHTS = {
    "completeness": 0.25,
    "freshness": 0.20,
    "official_source_ratio": 0.15,
    "cross_source_consistency": 0.15,
    "schema_validity": 0.10,
    "evidence_coverage": 0.10,
    "mean_agent_confidence": 0.05,
}


def calculate_quality_score(inputs: DataQualityInputs) -> float:
    weighted = sum(
        float(getattr(inputs, field)) * weight for field, weight in _QUALITY_WEIGHTS.items()
    )
    return round(100.0 * weighted, 6)


def calculate_total_score(
    *,
    fundamental_score: float,
    technical_score: float,
    sentiment_score: float,
    quality_confidence_score: float,
    dividend_bonus: float = 0.0,
    event_risk_multiplier: float = 1.0,
    market_score_adjustment: float = 0.0,
    market_risk_multiplier: float = 1.0,
    formula_version: str = FORMULA_VERSION,
) -> float:
    scores = {
        "fundamental": fundamental_score,
        "technical": technical_score,
        "sentiment": sentiment_score,
        "quality": quality_confidence_score,
    }
    if any(score < 0 or score > 100 for score in scores.values()):
        raise ValueError("all component scores must be within [0, 100]")
    if not 0 <= dividend_bonus <= 10:
        raise ValueError("dividend_bonus must be within [0, 10]")
    if not 0 <= event_risk_multiplier <= 1:
        raise ValueError("event_risk_multiplier must be within [0, 1]")
    if not -10 <= market_score_adjustment <= 10:
        raise ValueError("market_score_adjustment must be within [-10, 10]")
    if not 0.8 <= market_risk_multiplier <= 1:
        raise ValueError("market_risk_multiplier must be within [0.8, 1]")
    if formula_version not in {FORMULA_VERSION, FORMULA_VERSION_V2, FORMULA_VERSION_V3}:
        raise ValueError(f"unsupported scoring formula: {formula_version}")
    adjusted_fundamental = (
        min(100.0, fundamental_score + dividend_bonus)
        if formula_version in {FORMULA_VERSION_V2, FORMULA_VERSION_V3}
        else fundamental_score
    )
    base_total = (
        adjusted_fundamental * _COMPONENT_WEIGHTS["fundamental"]
        + technical_score * _COMPONENT_WEIGHTS["technical"]
        + sentiment_score * _COMPONENT_WEIGHTS["sentiment"]
        + quality_confidence_score * 0.10
    )
    event_multiplier = (
        event_risk_multiplier if formula_version in {FORMULA_VERSION_V2, FORMULA_VERSION_V3} else 1.0
    )
    market_adjustment = market_score_adjustment if formula_version == FORMULA_VERSION_V3 else 0.0
    market_multiplier = market_risk_multiplier if formula_version == FORMULA_VERSION_V3 else 1.0
    return round(max(0.0, min(100.0, base_total + market_adjustment)) * event_multiplier * market_multiplier, 6)


def calculate_base_total_score(
    *,
    fundamental_score: float,
    technical_score: float,
    sentiment_score: float,
    quality_confidence_score: float,
    dividend_bonus: float = 0.0,
    formula_version: str = FORMULA_VERSION,
) -> float:
    return calculate_total_score(
        fundamental_score=fundamental_score,
        technical_score=technical_score,
        sentiment_score=sentiment_score,
        quality_confidence_score=quality_confidence_score,
        dividend_bonus=dividend_bonus,
        event_risk_multiplier=1.0,
        market_score_adjustment=0.0,
        market_risk_multiplier=1.0,
        formula_version=formula_version,
    )


def build_market_index_snapshot(
    benchmark_returns: dict[str, dict[date, float]],
    trading_date: date,
    *,
    score_per_percent: float = 1.5,
    adjustment_cap: float = 8.0,
    bearish_multiplier_floor: float = 0.85,
) -> MarketIndexSnapshot:
    """Summarize only frozen benchmark series; never use post-run live quotes."""

    def cumulative(name: str, sessions: int) -> float | None:
        values = benchmark_returns.get(name, {})
        dates = sorted(day for day in values if day <= trading_date)
        if len(dates) < sessions:
            return None
        total = 1.0
        for day in dates[-sessions:]:
            total *= 1 + float(values[day])
        return total - 1

    performances = tuple(
        MarketIndexPerformance(
            name=name,
            symbol=symbol,
            return_1d=cumulative(name, 1),
            return_5d=cumulative(name, 5),
            return_20d=cumulative(name, 20),
        )
        for name, symbol in _MARKET_INDEX_SYMBOLS.items()
    )

    def weighted(field: str) -> float | None:
        if any(getattr(item, field) is None for item in performances):
            return None
        return sum(_MARKET_INDEX_WEIGHTS[item.name] * getattr(item, field) for item in performances)

    return_1d = weighted("return_1d")
    return_5d = weighted("return_5d")
    return_20d = weighted("return_20d")
    if return_5d is None or return_20d is None:
        regime, adjustment, multiplier = "UNKNOWN", 0.0, 1.0
    elif return_5d >= 0.01 and return_20d >= 0:
        regime, adjustment, multiplier = "RISK_ON", min(adjustment_cap, return_5d * 100 * score_per_percent), 1.0
    elif return_5d <= -0.01 and return_20d < 0:
        regime = "RISK_OFF"
        adjustment = max(-adjustment_cap, return_5d * 100 * score_per_percent)
        multiplier = 1.0 - (1.0 - bearish_multiplier_floor) * min(1.0, abs(return_5d) / 0.05)
    else:
        regime, adjustment, multiplier = "NEUTRAL", max(-adjustment_cap, min(adjustment_cap, return_5d * 100 * score_per_percent)), 1.0
    return MarketIndexSnapshot(
        trading_date=trading_date,
        indices=performances,
        composite_return_1d=return_1d,
        composite_return_5d=return_5d,
        composite_return_20d=return_20d,
        regime=regime,
        score_adjustment=round(adjustment, 6),
        risk_multiplier=round(multiplier, 6),
    )


def build_composite_score(
    *,
    symbol: str,
    trading_date: date,
    decision_at: AwareDatetime,
    component_results: Sequence[AgentComponentResult],
    quality_inputs: DataQualityInputs,
    feature_snapshot_id: UUID,
    formula_version: str = FORMULA_VERSION,
    dividend_bonus: float = 0.0,
    event_risk_multiplier: float = 1.0,
    market_index_snapshot: MarketIndexSnapshot | None = None,
) -> CompositeScore:
    by_component: dict[str, AgentComponentResult] = {}
    for result in component_results:
        if result.component in by_component:
            raise ValueError(f"duplicate component result: {result.component}")
        if any(evidence.available_at > decision_at for evidence in result.evidence):
            raise ValueError("future evidence cannot contribute to a composite score")
        by_component[result.component] = result
    if set(by_component) != set(_COMPONENT_WEIGHTS):
        raise ValueError("composite score requires fundamental, technical, and sentiment results")

    quality_score = calculate_quality_score(quality_inputs)
    fundamental = by_component["fundamental"].score
    technical = by_component["technical"].score
    sentiment = by_component["sentiment"].score
    if formula_version == FORMULA_VERSION_V3 and market_index_snapshot is None:
        raise ValueError("market formula requires a frozen market index snapshot")
    effective_dividend_bonus = (
        dividend_bonus if formula_version in {FORMULA_VERSION_V2, FORMULA_VERSION_V3} else 0.0
    )
    effective_risk_multiplier = (
        event_risk_multiplier if formula_version in {FORMULA_VERSION_V2, FORMULA_VERSION_V3} else 1.0
    )
    effective_market_snapshot = market_index_snapshot if formula_version == FORMULA_VERSION_V3 else None
    effective_market_adjustment = effective_market_snapshot.score_adjustment if effective_market_snapshot else 0.0
    effective_market_multiplier = effective_market_snapshot.risk_multiplier if effective_market_snapshot else 1.0
    adjusted_fundamental = min(100.0, fundamental + effective_dividend_bonus)
    base_total_score = calculate_base_total_score(
        fundamental_score=fundamental,
        technical_score=technical,
        sentiment_score=sentiment,
        quality_confidence_score=quality_score,
        dividend_bonus=effective_dividend_bonus,
        formula_version=formula_version,
    )
    evidence = tuple(
        item
        for component in ("fundamental", "technical", "sentiment")
        for item in by_component[component].evidence
    )
    return CompositeScore(
        symbol=symbol,
        trading_date=trading_date,
        decision_at=decision_at,
        fundamental_score=fundamental,
        technical_score=technical,
        sentiment_score=sentiment,
        quality_confidence_score=quality_score,
        adjusted_fundamental_score=adjusted_fundamental,
        base_total_score=base_total_score,
        dividend_bonus=effective_dividend_bonus,
        event_risk_multiplier=effective_risk_multiplier,
        market_index_snapshot=effective_market_snapshot,
        market_regime=effective_market_snapshot.regime if effective_market_snapshot else "UNKNOWN",
        market_score_adjustment=effective_market_adjustment,
        market_risk_multiplier=effective_market_multiplier,
        total_score=calculate_total_score(
            fundamental_score=fundamental,
            technical_score=technical,
            sentiment_score=sentiment,
            quality_confidence_score=quality_score,
            dividend_bonus=effective_dividend_bonus,
            event_risk_multiplier=effective_risk_multiplier,
            market_score_adjustment=effective_market_adjustment,
            market_risk_multiplier=effective_market_multiplier,
            formula_version=formula_version,
        ),
        formula_version=formula_version,
        agent_bundle_sha256=stable_hash(
            tuple(by_component[name] for name in ("fundamental", "technical", "sentiment"))
        ),
        feature_snapshot_id=feature_snapshot_id,
        evidence_bundle_sha256=stable_hash(evidence),
    )
