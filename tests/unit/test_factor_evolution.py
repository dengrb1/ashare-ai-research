from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ashare_ai.agents.decision.factor import FactorDecisionProvider
from ashare_ai.agents.decision.models import MarketState, UnifiedDecision
from ashare_ai.agents.decision.router import normalize_request_mode
from ashare_ai.quant.factor_evolution import (
    FactorEvaluationRow,
    FactorEvolutionService,
    FactorProposal,
)
from ashare_ai.quant.factor_registry import FactorGenomeRegistry
from ashare_ai.scoring.factor import DEFAULT_FACTOR_GENOME, FactorGenome
from ashare_ai.storage.models import Base


def _state() -> MarketState:
    return MarketState(
        symbol="600000.SH",
        trading_date=date(2026, 9, 25),
        available_at=datetime(2026, 9, 25, 15, tzinfo=UTC),
        open=10,
        high=10,
        low=10,
        close=10,
        volume=0,
        amount=0,
    )


def _rows(count: int = 60, *, forward_return: float = 0.01) -> list[FactorEvaluationRow]:
    start = datetime(2026, 1, 1, 15, tzinfo=UTC)
    return [
        FactorEvaluationRow(
            symbol="600000.SH",
            trading_date=(start + timedelta(days=index)).date().isoformat(),
            available_at=start + timedelta(days=index),
            decision_at=start + timedelta(days=index),
            fundamental_score=80,
            technical_score=80,
            sentiment_score=80,
            quality_confidence_score=80,
            forward_return=forward_return,
        )
        for index in range(count)
    ]


def _proposal(**updates: object) -> FactorProposal:
    values = {
        "hypothesis": "raise the quality weight",
        "risk_notes": "limited test horizon",
        "explanation": "bounded parameter change",
        **DEFAULT_FACTOR_GENOME.model_dump(exclude={"schema_version", "formula_version", "mutation_seed"}),
        "quality_weight": 0.15,
        "sentiment_weight": 0.15,
    }
    values.update(updates)
    return FactorProposal.model_validate(values)


@pytest.mark.asyncio
async def test_factor_decision_is_stable_and_legacy_is_only_an_alias() -> None:
    provider = FactorDecisionProvider()
    first = await provider.predict(_state())
    second = await provider.predict(_state())
    assert first.model_dump_json() == second.model_dump_json()
    assert first.mode == "factor"
    assert first.model_version == "factor-v1.0.0"
    assert first.parameter_sha256 == DEFAULT_FACTOR_GENOME.parameter_sha256
    assert 0 <= first.position <= DEFAULT_FACTOR_GENOME.max_position
    assert normalize_request_mode("legacy") == "factor"
    with pytest.raises(ValueError, match="has been removed"):
        normalize_request_mode("jev")
    historical = first.model_dump()
    historical.update(mode="jev", model_version="jev-baseline-v1")
    assert UnifiedDecision.model_validate(historical).mode == "jev"


def test_genome_and_proposal_reject_unsafe_parameters_and_outputs() -> None:
    with pytest.raises(ValidationError, match="sum to 1"):
        FactorGenome(fundamental_weight=0.4)
    with pytest.raises(ValidationError, match="buy_threshold"):
        FactorGenome(buy_threshold=52, sell_threshold=50)
    with pytest.raises(ValidationError):
        _proposal(target_price=12)
    with pytest.raises(ValidationError, match="trade instruction"):
        _proposal(hypothesis="buy now")
    with pytest.raises(ValidationError, match="shadow weights"):
        FactorGenome(momentum_weight=0.05, fundamental_weight=0.35, technical_weight=0.30)


def test_evolution_gates_and_pit() -> None:
    service = FactorEvolutionService(minimum_samples=60, maximum_drawdown=0.2)
    decision_at = datetime(2026, 9, 25, 18, tzinfo=UTC)
    proposal = _proposal()
    accepted = service.evaluate(
        rows=_rows(), baseline=DEFAULT_FACTOR_GENOME,
        decision_at=decision_at, proposals=[proposal],
    )
    assert len(accepted.candidates) == 1
    assert accepted.candidates[0].status == "CANDIDATE"
    assert not service.evaluate(
        rows=_rows(59), baseline=DEFAULT_FACTOR_GENOME,
        decision_at=decision_at, proposals=[proposal],
    ).candidates
    future = _rows()
    future[0] = future[0].model_copy(update={"available_at": decision_at + timedelta(days=1)})
    with pytest.raises(ValueError, match="after decision_at"):
        service.evaluate(rows=future, baseline=DEFAULT_FACTOR_GENOME,
                         decision_at=decision_at, proposals=[proposal])


def test_registry_approval_rejection_and_default_rollback() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = FactorEvolutionService()
    result = service.evaluate(
        rows=_rows(), baseline=DEFAULT_FACTOR_GENOME,
        decision_at=datetime(2026, 9, 25, 18, tzinfo=UTC), proposals=[_proposal()],
    )
    with Session(engine) as session:
        registry = FactorGenomeRegistry(session)
        stored = registry.store_candidates(result.candidates)
        session.commit()
        assert registry.store_candidates(result.candidates) == stored
        approved = registry.approve(stored[0], user_id=None)
        session.commit()
        assert approved["candidate_id"] == stored[0]
        assert registry.active_genome() == result.candidates[0].genome
        restored = registry.rollback(user_id=None)
        session.commit()
        assert restored["is_default"] is True
        assert registry.active_genome() == DEFAULT_FACTOR_GENOME
