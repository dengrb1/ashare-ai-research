from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ashare_ai.agents.protocols import AgentRequest
from ashare_ai.agents.validation import (
    ComponentAnalysis,
    validate_component_payload,
    validate_manager_payload,
)
from ashare_ai.core.contracts import (
    AgentComponentResult,
    AvailabilityBasis,
    DailyBar,
    DataQualityInputs,
    EvidenceRef,
)
from ashare_ai.features.technical import extract_technical_features
from ashare_ai.scoring.formula import build_composite_score, calculate_quality_score

SHANGHAI = ZoneInfo("Asia/Shanghai")
HASH = "b" * 64
DECISION_AT = datetime(2026, 7, 15, 18, tzinfo=SHANGHAI)


def test_agent_schema_forbids_unknown_fields_and_future_evidence() -> None:
    evidence = _evidence("source-1", DECISION_AT - timedelta(minutes=1))
    request = AgentRequest(
        component="fundamental",
        symbol="600000.SH",
        decision_at=DECISION_AT,
        prompt_version="v1",
        features={"roe": 0.12},
        evidence=(evidence,),
    )
    payload = _agent_payload("fundamental", evidence)
    assert validate_component_payload(payload, request=request).score == 80

    invalid = dict(payload, invented_field=True)
    with pytest.raises(ValidationError, match="invented_field"):
        validate_component_payload(invalid, request=request)

    future = _evidence("future", DECISION_AT + timedelta(seconds=1))
    future_request = request.model_copy(update={"evidence": (future,)})
    with pytest.raises(ValueError, match="future"):
        validate_component_payload(_agent_payload("fundamental", future), request=future_request)


def test_component_analysis_schema_requires_score_and_confidence_ranges() -> None:
    evidence = _evidence("source-1", DECISION_AT - timedelta(minutes=1))
    valid = {
        "component": "fundamental",
        "score": 72,
        "confidence": 0.72,
        "evidence": [evidence.model_dump()],
    }
    assert ComponentAnalysis.model_validate(valid).score == 72
    with pytest.raises(ValidationError, match="less than or equal to 100"):
        ComponentAnalysis.model_validate(dict(valid, score=101))
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        ComponentAnalysis.model_validate(dict(valid, confidence=1.01))


def test_manager_can_only_summarize() -> None:
    conclusion = validate_manager_payload(
        {"summary": "基本面改善但事件风险仍需观察", "thesis": ["现金流改善"]}
    )
    assert conclusion.summary.startswith("基本面")
    with pytest.raises(ValueError, match="forbidden decision field"):
        validate_manager_payload({"summary": "观察", "total_score": 88})
    with pytest.raises(ValueError, match="must not set"):
        validate_manager_payload({"summary": "最终评分：88"})


def test_versioned_formula_is_deterministic_35_35_20_10() -> None:
    evidence = _evidence("shared", DECISION_AT - timedelta(minutes=1))
    components = tuple(
        AgentComponentResult.model_validate(_agent_payload(component, evidence, score=score))
        for component, score in (
            ("fundamental", 80),
            ("technical", 60),
            ("sentiment", 50),
        )
    )
    quality_inputs = DataQualityInputs(
        completeness=1,
        freshness=1,
        official_source_ratio=1,
        cross_source_consistency=1,
        schema_validity=1,
        evidence_coverage=1,
        mean_agent_confidence=1,
    )
    assert calculate_quality_score(quality_inputs) == 100
    result = build_composite_score(
        symbol="600000.SH",
        trading_date=date(2026, 7, 15),
        decision_at=DECISION_AT,
        component_results=components,
        quality_inputs=quality_inputs,
        feature_snapshot_id=uuid4(),
    )
    assert result.total_score == 69


def test_technical_features_ignore_records_available_after_cutoff() -> None:
    start = date(2026, 6, 15)
    bars = [_bar(start + timedelta(days=index), Decimal(10 + index) / 10) for index in range(31)]
    baseline = extract_technical_features(
        bars,
        decision_at=DECISION_AT,
        trading_date=date(2026, 7, 15),
        symbol="600000.SH",
    )
    future_bar = _bar(
        date(2026, 7, 15),
        Decimal("99"),
        available_at=DECISION_AT + timedelta(seconds=1),
        source_record_id="future-revision",
    )
    repeated = extract_technical_features(
        [*bars, future_bar],
        decision_at=DECISION_AT,
        trading_date=date(2026, 7, 15),
        symbol="600000.SH",
    )
    assert repeated == baseline


def _agent_payload(
    component: str, evidence: EvidenceRef, *, score: float = 80
) -> dict[str, object]:
    return {
        "component": component,
        "score": score,
        "confidence": 0.8,
        "evidence": [evidence.model_dump()],
        "positive_factors": ["test"],
        "negative_factors": [],
        "risk_flags": [],
        "model_provider": "fixture",
        "model_name": "fixture-model",
        "reasoning_effort": "high",
        "prompt_version": "v1",
        "prompt_sha256": HASH,
        "response_sha256": HASH,
        "input_tokens": 10,
        "output_tokens": 5,
        "duration_ms": 1,
        "retry_count": 0,
    }


def _evidence(record_id: str, available_at: datetime) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=record_id,
        evidence_type="feature",
        source="fixture",
        source_record_id=record_id,
        available_at=available_at,
        payload_sha256=HASH,
    )


def _bar(
    trading_date: date,
    close: Decimal,
    *,
    available_at: datetime | None = None,
    source_record_id: str | None = None,
) -> DailyBar:
    available_at = available_at or datetime.combine(
        trading_date, datetime.min.time().replace(hour=17), tzinfo=SHANGHAI
    )
    return DailyBar(
        symbol="600000.SH",
        trading_date=trading_date,
        available_at=available_at,
        source="fixture",
        source_record_id=source_record_id or trading_date.isoformat(),
        fetched_at=available_at,
        payload_sha256=HASH,
        adapter_version="test",
        ingestion_run_id=uuid4(),
        availability_basis=AvailabilityBasis.VENDOR_TIMESTAMP,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1000"),
        amount=Decimal("10000"),
    )
