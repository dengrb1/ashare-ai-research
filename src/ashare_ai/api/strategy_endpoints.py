"""Administrator API for the low-frequency factor-genome evolution.

Every endpoint here is read-only with respect to research results: approving a
candidate only moves the active version pointer, and rolling back restores the
version it replaced.  No endpoint can place an order or rewrite a stored score.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ashare_ai.api.auth import AuthContext, require_admin
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.core.system_settings import SystemConfigurationService, get_effective_settings
from ashare_ai.quant.factor_evolution import (
    FactorEvaluationRow,
    FactorEvolutionService,
    FactorProposal,
)
from ashare_ai.quant.factor_registry import FactorGenomeRegistry
from ashare_ai.scoring.factor import DEFAULT_FACTOR_GENOME, FactorGenome

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])

DbSession = Annotated[Session, Depends(get_db)]
Reader = Annotated[AuthContext, Depends(get_auth_context)]
Writer = Annotated[AuthContext, Depends(get_write_context)]

# The weekly job is enabled or disabled through the audited system settings, so
# the toggle is a normal settings write rather than a bespoke flag.
EVOLUTION_ENABLED_FIELD = "strategy_evolution_enabled"


class EvolutionRequest(BaseModel):
    """Run one evolution pass over a frozen dataset.

    The dataset rows are point-in-time observations; any row whose evidence was
    not available at ``decision_at`` is rejected before evaluation.
    """

    dataset: list[dict[str, Any]] = Field(min_length=1, max_length=5000)
    baseline: FactorGenome = Field(default_factory=lambda: DEFAULT_FACTOR_GENOME)
    decision_at: datetime
    proposals: list[FactorProposal] = Field(default_factory=list, max_length=8)
    persist: bool = Field(default=True, description="Store passing candidates for review")


class EvolutionResponse(BaseModel):
    status: str
    champion: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    stored_candidate_ids: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class EvolutionToggleRequest(BaseModel):
    enabled: bool


def _service() -> FactorEvolutionService:
    config = get_effective_settings()
    return FactorEvolutionService(
        minimum_samples=config.strategy_evolution_min_samples,
        maximum_drawdown=config.strategy_evolution_max_drawdown,
        maximum_turnover=config.strategy_evolution_max_turnover,
        minimum_improvement=config.strategy_evolution_min_improvement,
        maximum_candidates=config.strategy_evolution_max_candidates,
    )


@router.post("/evolution", response_model=EvolutionResponse)
def evolve_factor(
    payload: EvolutionRequest,
    db: DbSession,
    context: Writer,
) -> EvolutionResponse:
    """Evaluate the baseline plus every proposal and store passing candidates."""

    require_admin(context)
    try:
        rows = [FactorEvaluationRow.model_validate(row) for row in payload.dataset]
        result = _service().evaluate(
            rows=rows,
            baseline=payload.baseline,
            decision_at=payload.decision_at,
            proposals=payload.proposals,
        )
        stored: list[str] = []
        if payload.persist and result.candidates:
            by_hash = {proposal.proposal_sha256: proposal for proposal in payload.proposals}
            proposals = {
                candidate.candidate_id: by_hash[candidate.proposal_sha256]
                for candidate in result.candidates
                if candidate.proposal_sha256 in by_hash
            }
            stored = FactorGenomeRegistry(db).store_candidates(
                result.candidates, proposals=proposals
            )
            db.commit()
        return EvolutionResponse(
            status="CANDIDATES_READY" if result.candidates else "NO_CANDIDATE",
            champion=result.champion.model_dump(mode="json") if result.champion else None,
            candidates=[item.model_dump(mode="json") for item in result.candidates],
            rejected=[item.model_dump(mode="json") for item in result.rejected],
            stored_candidate_ids=stored,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/evolution/active")
def active_version(db: DbSession, context: Reader) -> dict[str, Any]:
    """Return the currently active factor genome version."""

    require_admin(context)
    return FactorGenomeRegistry(db).active_version()


@router.get("/evolution/candidates")
def list_candidates(
    db: DbSession,
    context: Reader,
    status: Literal["CANDIDATE", "ACTIVE", "REJECTED", "ROLLED_BACK"] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List stored factor genome revisions with their backtest metrics."""

    require_admin(context)
    versions = FactorGenomeRegistry(db).list_versions(status=status, limit=limit)
    return {"versions": versions, "count": len(versions)}


@router.get("/evolution/candidates/{candidate_id}")
def get_candidate(candidate_id: str, db: DbSession, context: Reader) -> dict[str, Any]:
    """Return one stored revision, including the LLM proposal text."""

    require_admin(context)
    version = FactorGenomeRegistry(db).get_version(candidate_id)
    if version is None:
        raise HTTPException(status_code=404, detail="factor genome candidate not found")
    return version


@router.post("/evolution/candidates/{candidate_id}/approve")
def approve_candidate(
    candidate_id: str,
    payload: ReviewRequest,
    db: DbSession,
    context: Writer,
) -> dict[str, Any]:
    """Activate a candidate.  Historical research results are not modified."""

    require_admin(context)
    try:
        result = FactorGenomeRegistry(db).approve(
            candidate_id, user_id=context.user.user_id, note=payload.note
        )
        db.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/evolution/candidates/{candidate_id}/reject")
def reject_candidate(
    candidate_id: str,
    payload: ReviewRequest,
    db: DbSession,
    context: Writer,
) -> dict[str, Any]:
    """Reject a pending candidate without changing the active version."""

    require_admin(context)
    try:
        result = FactorGenomeRegistry(db).reject(
            candidate_id, user_id=context.user.user_id, note=payload.note
        )
        db.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/evolution/rollback")
def rollback_version(
    payload: ReviewRequest,
    db: DbSession,
    context: Writer,
) -> dict[str, Any]:
    """Restore the factor genome version that the active one replaced."""

    require_admin(context)
    try:
        result = FactorGenomeRegistry(db).rollback(user_id=context.user.user_id, note=payload.note)
        db.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/evolution/schedule")
def evolution_schedule(context: Reader) -> dict[str, Any]:
    """Report whether the weekly evolution task is enabled and when it runs."""

    require_admin(context)
    config = get_effective_settings()
    return {
        "enabled": config.strategy_evolution_enabled,
        "weekday": config.strategy_evolution_weekday,
        "hour": config.strategy_evolution_hour,
        "minimum_samples": config.strategy_evolution_min_samples,
        "maximum_drawdown": config.strategy_evolution_max_drawdown,
        "maximum_turnover": config.strategy_evolution_max_turnover,
        "minimum_improvement": config.strategy_evolution_min_improvement,
        "maximum_candidates": config.strategy_evolution_max_candidates,
    }


@router.post("/evolution/schedule")
def update_evolution_schedule(
    payload: EvolutionToggleRequest,
    db: DbSession,
    context: Writer,
) -> dict[str, Any]:
    """Enable or disable the weekly evolution task through audited settings."""

    require_admin(context)
    SystemConfigurationService().save(
        db,
        public_updates={EVOLUTION_ENABLED_FIELD: payload.enabled},
        secret_updates={},
        user_id=context.user.user_id,
    )
    db.commit()
    return evolution_schedule(context)
