from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from ashare_ai.core.contracts import (
    AgentComponentResult,
    AvailabilityBasis,
    CashDividend,
    CompositeScore,
    DataQualityInputs,
    Disclosure,
    EvidenceRef,
    NewsItem,
)
from ashare_ai.core.time import SHANGHAI
from ashare_ai.portfolio.events import (
    EventRiskPolicy,
    EventSeverity,
    aggregate_event_risk,
    classify_event_risks,
)
from ashare_ai.scoring.dividends import calculate_dividend_bonus
from ashare_ai.scoring.formula import FORMULA_VERSION_V2, build_composite_score

HASH = "a" * 64
DECISION_AT = datetime(2026, 7, 15, 18, tzinfo=SHANGHAI)


def _pit(symbol: str, available_at: datetime, record_id: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "trading_date": date(2026, 7, 15),
        "available_at": available_at,
        "source": "fixture",
        "source_record_id": record_id,
        "fetched_at": DECISION_AT,
        "payload_sha256": HASH,
        "adapter_version": "fixture-v1",
        "ingestion_run_id": uuid4(),
        "availability_basis": AvailabilityBasis.VENDOR_TIMESTAMP,
    }


def _dividend(year: int, payment_date: date, amount: str, *, future: bool = False) -> CashDividend:
    available_at = (
        DECISION_AT + timedelta(minutes=1)
        if future
        else DECISION_AT - timedelta(days=30)
    )
    return CashDividend(
        **_pit("600000.SH", available_at, f"dividend-{year}"),
        dividend_id=f"dividend-{year}",
        fiscal_year=year,
        implementation_announcement_date=available_at.date(),
        payment_date=payment_date,
        cash_dividend_per_share=Decimal(amount),
        official_verified=True,
    )


def test_dividend_bonus_uses_only_implemented_pit_cash_dividends() -> None:
    result = calculate_dividend_bonus(
        [
            _dividend(2025, date(2026, 6, 1), "2"),
            _dividend(2024, date(2025, 6, 1), "1"),
            _dividend(2023, date(2024, 6, 1), "1"),
            _dividend(2026, date(2026, 8, 1), "10"),
            _dividend(2022, date(2023, 6, 1), "10", future=True),
        ],
        symbol="600000.SH",
        decision_at=DECISION_AT,
        frozen_close=Decimal("100"),
    )

    assert result.cash_dividend_per_share_365d == Decimal("2")
    assert result.dividend_yield_365d == Decimal("0.02")
    assert result.yield_bonus == 4
    assert result.consecutive_dividend_years == 3
    assert result.continuity_bonus == 1
    assert result.total_bonus == 5


def test_media_negative_is_medium_but_official_critical_blocks() -> None:
    official = Disclosure(
        **_pit("600000.SH", DECISION_AT - timedelta(days=1), "official"),
        announcement_id="official",
        title="关于重大违法强制退市的公告",
        published_at=DECISION_AT - timedelta(days=1),
        official_verified=True,
        official_source="CNINFO",
        document_uri="https://example.test/official",
        document_sha256=HASH,
    )
    media = NewsItem(
        **_pit("600000.SH", DECISION_AT - timedelta(days=2), "media"),
        news_id="media",
        title="媒体称公司可能财务造假",
        published_at=DECISION_AT - timedelta(days=2),
        publisher="FREE_MEDIA",
        content_sha256=HASH,
        related_symbols=("600000.SH",),
    )
    policy = EventRiskPolicy(
        version="v2",
        multipliers={
            EventSeverity.LOW: 1,
            EventSeverity.MEDIUM: 0.8,
            EventSeverity.HIGH: 0.5,
            EventSeverity.CRITICAL: 0,
        },
        blocked_severities=frozenset({EventSeverity.CRITICAL}),
    )

    media_only = classify_event_risks(
        [], [media], symbol="600000.SH", decision_at=DECISION_AT
    )
    assert media_only[0].severity == EventSeverity.MEDIUM
    assert aggregate_event_risk(media_only, policy).multiplier == 0.8
    combined = classify_event_risks(
        [official], [media], symbol="600000.SH", decision_at=DECISION_AT
    )
    decision = aggregate_event_risk(combined, policy)
    assert decision.multiplier == 0
    assert decision.block_new is True


def test_generic_official_risk_notice_does_not_become_high_risk() -> None:
    disclosure = Disclosure(
        **_pit("600000.SH", DECISION_AT - timedelta(days=1), "ordinary-risk"),
        announcement_id="ordinary-risk",
        title="关于公司经营风险提示的公告",
        published_at=DECISION_AT - timedelta(days=1),
        official_verified=True,
        official_source="CNINFO",
        document_uri="https://example.test/ordinary-risk",
        document_sha256=HASH,
        category_codes=("RISK",),
    )

    assert classify_event_risks(
        [disclosure], [], symbol="600000.SH", decision_at=DECISION_AT
    ) == ()


def _component(name: str, score: float) -> AgentComponentResult:
    evidence = EvidenceRef(
        evidence_id=name,
        evidence_type="fixture",
        source="fixture",
        source_record_id=name,
        available_at=DECISION_AT,
        payload_sha256=HASH,
    )
    return AgentComponentResult(
        component=name,  # type: ignore[arg-type]
        score=score,
        confidence=1,
        evidence=(evidence,),
        model_provider="fixture",
        model_name="fixture",
        reasoning_effort="none",
        prompt_version="v1",
        prompt_sha256=HASH,
        response_sha256=HASH,
        input_tokens=0,
        output_tokens=0,
        duration_ms=0,
        retry_count=0,
    )


def test_v2_score_exposes_base_bonus_and_final_risk_adjustment() -> None:
    score = build_composite_score(
        symbol="600000.SH",
        trading_date=date(2026, 7, 15),
        decision_at=DECISION_AT,
        component_results=(
            _component("fundamental", 80),
            _component("technical", 70),
            _component("sentiment", 60),
        ),
        quality_inputs=DataQualityInputs(
            completeness=1,
            freshness=1,
            official_source_ratio=1,
            cross_source_consistency=1,
            schema_validity=1,
            evidence_coverage=1,
            mean_agent_confidence=1,
        ),
        feature_snapshot_id=uuid4(),
        formula_version=FORMULA_VERSION_V2,
        dividend_bonus=5,
        event_risk_multiplier=0.5,
    )

    assert score.adjusted_fundamental_score == 85
    assert score.base_total_score == 76.25
    assert score.total_score == 38.125
    assert score.dividend_bonus == 5
    assert score.event_risk_multiplier == 0.5


def test_historical_v1_composite_artifact_fills_new_fields() -> None:
    payload = {
        "symbol": "600000.SH",
        "trading_date": "2025-01-01",
        "decision_at": "2025-01-01T18:00:00+08:00",
        "fundamental_score": 70,
        "technical_score": 70,
        "sentiment_score": 70,
        "quality_confidence_score": 70,
        "total_score": 70,
        "formula_version": "composite-35-35-20-10-v1",
        "agent_bundle_sha256": HASH,
        "feature_snapshot_id": str(uuid4()),
        "evidence_bundle_sha256": HASH,
    }
    score = CompositeScore.model_validate(payload)
    assert score.base_total_score == 70
    assert score.adjusted_fundamental_score == 70
    assert score.dividend_bonus == 0
    assert score.event_risk_multiplier == 1
