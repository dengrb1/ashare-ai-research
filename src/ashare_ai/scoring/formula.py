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
) -> float:
    scores = {
        "fundamental": fundamental_score,
        "technical": technical_score,
        "sentiment": sentiment_score,
        "quality": quality_confidence_score,
    }
    if any(score < 0 or score > 100 for score in scores.values()):
        raise ValueError("all component scores must be within [0, 100]")
    total = (
        fundamental_score * _COMPONENT_WEIGHTS["fundamental"]
        + technical_score * _COMPONENT_WEIGHTS["technical"]
        + sentiment_score * _COMPONENT_WEIGHTS["sentiment"]
        + quality_confidence_score * 0.10
    )
    return round(total, 6)


def build_composite_score(
    *,
    symbol: str,
    trading_date: date,
    decision_at: AwareDatetime,
    component_results: Sequence[AgentComponentResult],
    quality_inputs: DataQualityInputs,
    feature_snapshot_id: UUID,
    formula_version: str = FORMULA_VERSION,
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
        total_score=calculate_total_score(
            fundamental_score=fundamental,
            technical_score=technical,
            sentiment_score=sentiment,
            quality_confidence_score=quality_score,
        ),
        formula_version=formula_version,
        agent_bundle_sha256=stable_hash(
            tuple(by_component[name] for name in ("fundamental", "technical", "sentiment"))
        ),
        feature_snapshot_id=feature_snapshot_id,
        evidence_bundle_sha256=stable_hash(evidence),
    )
