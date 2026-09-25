"""Bounded research-only strategy evolution endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ashare_ai.api.auth import AuthContext
from ashare_ai.api.dependencies import get_auth_context
from ashare_ai.quant.evolution import EvolutionMetrics, StrategyEvolutionService, StrategyGenome

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])


class EvolutionRequest(BaseModel):
    dataset: list[dict[str, Any]] = Field(min_length=1, max_length=5000)
    baseline: StrategyGenome = Field(default_factory=StrategyGenome)
    model_version: str = Field(default="jev-baseline-v1", min_length=1, max_length=128)
    decision_at: datetime
    generations: int = Field(default=3, ge=1, le=12)
    population_size: int = Field(default=8, ge=1, le=32)


@router.post("/evolution")
def evolve_strategy(payload: EvolutionRequest, _: AuthContext = Depends(get_auth_context)) -> dict[str, Any]:
    def evaluate(genome: StrategyGenome, rows: list[dict[str, Any]]) -> EvolutionMetrics:
        returns = [float(row.get("return_1d", row.get("features", {}).get("return_1d", 0.0))) for row in rows]
        selected = [value for value in returns if value >= genome.entry_threshold]
        net_return = sum(selected) * genome.risk_budget
        downside = sum(value for value in selected if value < 0)
        turnover = len(selected) / max(1, len(returns)) * 20.0
        return EvolutionMetrics(sample_count=len(returns), net_return=net_return, sharpe=net_return / max(0.01, abs(downside) + 0.01), maximum_drawdown=min(1.0, abs(downside)), turnover=turnover)

    try:
        result = StrategyEvolutionService().evolve(dataset=payload.dataset, baseline=payload.baseline, model_version=payload.model_version, decision_at=payload.decision_at, evaluator=evaluate, generations=payload.generations, population_size=payload.population_size)
        return {"status": "ACTIVE_CANDIDATE" if result.champion else "NO_CHAMPION", "champion": result.champion.model_dump(mode="json") if result.champion else None, "candidates": [item.model_dump(mode="json") for item in result.candidates]}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
