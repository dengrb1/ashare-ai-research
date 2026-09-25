from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from ashare_ai.agents.decision.models import (
    ActionProbabilities,
    BinaryProbabilities,
    DecisionProbabilities,
    DirectionProbabilities,
    MarketState,
    RiskProbabilities,
    UnifiedDecision,
)
from ashare_ai.agents.decision.router import DecisionRouter


def _market_state() -> MarketState:
    return MarketState(
        symbol="600000.SH",
        trading_date=date(2026, 9, 25),
        available_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        open=10.0,
        high=10.5,
        low=9.8,
        close=10.2,
        volume=1_000_000,
        amount=10_000_000,
    )


def _decision(*, confidence: float, mode: str = "legacy") -> UnifiedDecision:
    return UnifiedDecision(
        symbol="600000.SH",
        trading_date=date(2026, 9, 25),
        available_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        decision_at=datetime(2026, 9, 25, 18, 0, tzinfo=UTC),
        probabilities=DecisionProbabilities(
            direction_1d=DirectionProbabilities(UP=confidence, FLAT=(1 - confidence) / 2, DOWN=(1 - confidence) / 2),
            direction_5d=DirectionProbabilities(UP=confidence, FLAT=(1 - confidence) / 2, DOWN=(1 - confidence) / 2),
            up_over_3pct_5d=BinaryProbabilities(YES=confidence, NO=1 - confidence),
            action=ActionProbabilities(BUY=(1 - confidence) / 2, HOLD=confidence, SELL=(1 - confidence) / 2),
            risk=RiskProbabilities(LOW=(1 - confidence) / 2, MEDIUM=confidence, HIGH=(1 - confidence) / 2),
        ),
        action="HOLD",
        risk="MEDIUM",
        position=20,
        mode=mode,
        model_version="test-v1",
        input_manifest_hash="0" * 64,
    )


@pytest.mark.asyncio
async def test_low_confidence_jev_decision_is_queued_for_system2() -> None:
    enqueue = Mock(return_value="system2-job-1")
    provider = AsyncMock()
    provider.predict.return_value = _decision(confidence=0.34, mode="jev")
    router = DecisionRouter(
        mode="jev",
        fallback_enabled=False,
        confidence_threshold=0.60,
        system2_enqueue=enqueue,
    )
    router._primary_provider = provider

    decision = await router.predict(_market_state())

    assert decision.mode == "jev"
    assert router.system2_state == "QUEUED"
    assert router.system2_job_id == "system2-job-1"
    assert enqueue.call_args.args[1] == "LOW_CONFIDENCE"


@pytest.mark.asyncio
async def test_jev_unavailable_queues_system2_before_legacy_fallback() -> None:
    enqueue = Mock(return_value="system2-job-2")
    primary = AsyncMock()
    primary.predict.side_effect = RuntimeError("missing checkpoint")
    fallback = AsyncMock()
    fallback.predict.return_value = _decision(confidence=0.9)
    router = DecisionRouter(
        mode="jev",
        fallback_enabled=True,
        system2_enqueue=enqueue,
    )
    router._primary_provider = primary
    router._fallback_provider = fallback

    decision = await router.predict(_market_state())

    assert decision.mode == "legacy"
    assert router.system2_state == "QUEUED"
    assert enqueue.call_args.args[1] == "JEV_UNAVAILABLE"


@pytest.mark.asyncio
async def test_system2_disabled_is_reported_without_queueing() -> None:
    provider = AsyncMock()
    provider.predict.return_value = _decision(confidence=0.34)
    router = DecisionRouter(
        mode="legacy",
        fallback_enabled=False,
        system2_enabled=False,
    )
    router._primary_provider = provider

    await router.predict(_market_state())

    assert router.system2_state == "DISABLED"
    assert router.system2_job_id is None
