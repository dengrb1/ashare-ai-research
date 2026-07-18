from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from pydantic import AwareDatetime

from ashare_ai.core.contracts import (
    AgentComponentResult,
    CompositeScore,
    DataQualityInputs,
)
from ashare_ai.core.hashing import stable_hash

FORMULA_VERSION = "composite-35-35-20-10-v1"
FORMULA_VERSION_V2 = "composite-35-35-20-10-dividend-news-v2"
QUALITY_VERSION = "quality-v1"

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
    if formula_version not in {FORMULA_VERSION, FORMULA_VERSION_V2}:
        raise ValueError(f"unsupported scoring formula: {formula_version}")
    adjusted_fundamental = (
        min(100.0, fundamental_score + dividend_bonus)
        if formula_version == FORMULA_VERSION_V2
        else fundamental_score
    )
    base_total = (
        adjusted_fundamental * _COMPONENT_WEIGHTS["fundamental"]
        + technical_score * _COMPONENT_WEIGHTS["technical"]
        + sentiment_score * _COMPONENT_WEIGHTS["sentiment"]
        + quality_confidence_score * 0.10
    )
    multiplier = event_risk_multiplier if formula_version == FORMULA_VERSION_V2 else 1.0
    return round(base_total * multiplier, 6)


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
        formula_version=formula_version,
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
    effective_dividend_bonus = dividend_bonus if formula_version == FORMULA_VERSION_V2 else 0.0
    effective_risk_multiplier = (
        event_risk_multiplier if formula_version == FORMULA_VERSION_V2 else 1.0
    )
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
        total_score=calculate_total_score(
            fundamental_score=fundamental,
            technical_score=technical,
            sentiment_score=sentiment,
            quality_confidence_score=quality_score,
            dividend_bonus=effective_dividend_bonus,
            event_risk_multiplier=effective_risk_multiplier,
            formula_version=formula_version,
        ),
        formula_version=formula_version,
        agent_bundle_sha256=stable_hash(
            tuple(by_component[name] for name in ("fundamental", "technical", "sentiment"))
        ),
        feature_snapshot_id=feature_snapshot_id,
        evidence_bundle_sha256=stable_hash(evidence),
    )
