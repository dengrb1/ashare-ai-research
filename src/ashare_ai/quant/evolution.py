"""Auditable, research-only strategy and model evolution primitives.

Evolution operates on frozen historical rows supplied by the caller.  It never
places orders and a candidate cannot replace the active champion without
passing explicit out-of-sample gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Sequence

from pydantic import BaseModel, Field, model_validator

from ashare_ai.core.hashing import stable_hash


class StrategyGenome(BaseModel):
    schema_version: str = "strategy-genome-v1"
    features: tuple[str, ...] = ("return_1d", "return_5d", "volume_ratio", "rsi")
    entry_threshold: float = Field(default=0.02, ge=-1, le=1)
    exit_threshold: float = Field(default=-0.02, ge=-1, le=1)
    risk_budget: float = Field(default=0.20, gt=0, le=1)
    max_holding_sessions: int = Field(default=20, ge=1, le=240)
    mutation_seed: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> StrategyGenome:
        if self.exit_threshold >= self.entry_threshold:
            raise ValueError("exit_threshold must be below entry_threshold")
        return self


class EvolutionMetrics(BaseModel):
    sample_count: int = Field(ge=0)
    net_return: float
    sharpe: float
    maximum_drawdown: float = Field(ge=0)
    turnover: float = Field(ge=0)


class EvolutionCandidate(BaseModel):
    candidate_id: str
    status: str = "CANDIDATE"
    genome: StrategyGenome
    metrics: EvolutionMetrics
    dataset_sha256: str
    model_version: str
    created_at: datetime
    decision_at: datetime


@dataclass(frozen=True)
class EvolutionResult:
    champion: EvolutionCandidate | None
    candidates: tuple[EvolutionCandidate, ...]


class StrategyEvolutionService:
    def __init__(self, *, minimum_samples: int = 60, maximum_drawdown: float = 0.20, minimum_improvement: float = 0.0, maximum_turnover: float = 20.0):
        self.minimum_samples = minimum_samples
        self.maximum_drawdown = maximum_drawdown
        self.minimum_improvement = minimum_improvement
        self.maximum_turnover = maximum_turnover

    @staticmethod
    def mutate(genome: StrategyGenome, *, seed: int, generation: int = 1) -> StrategyGenome:
        # A small deterministic pseudo-random walk makes runs reproducible and
        # avoids hidden global RNG state in worker processes.
        value = (seed * 1103515245 + generation * 12345) & 0x7FFFFFFF
        delta = ((value % 2001) - 1000) / 100000
        risk_delta = (((value // 2001) % 1001) - 500) / 100000
        return genome.model_copy(update={
            "entry_threshold": max(-1.0, min(1.0, genome.entry_threshold + delta)),
            "exit_threshold": max(-1.0, min(genome.entry_threshold - 0.001, genome.exit_threshold + delta / 2)),
            "risk_budget": max(0.01, min(1.0, genome.risk_budget + risk_delta)),
            "mutation_seed": value,
        })

    def evolve(
        self,
        *,
        dataset: Sequence[dict[str, Any]],
        baseline: StrategyGenome,
        model_version: str,
        decision_at: datetime,
        evaluator: Callable[[StrategyGenome, Sequence[dict[str, Any]]], EvolutionMetrics],
        generations: int = 3,
        population_size: int = 8,
    ) -> EvolutionResult:
        for row in dataset:
            available = row.get("available_at")
            if isinstance(available, str):
                try:
                    available = datetime.fromisoformat(available.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("evolution dataset contains an invalid available_at") from exc
            if available is not None and available > decision_at:
                raise ValueError("evolution dataset contains evidence after decision_at")
        dataset_hash = stable_hash(list(dataset))
        candidates: list[EvolutionCandidate] = []
        population = [baseline]
        for generation in range(max(1, generations)):
            population = [self.mutate(genome, seed=genome.mutation_seed + index + 1, generation=generation) for index, genome in enumerate(population)]
            population = population[: max(1, population_size)]
            for genome in population:
                metrics = evaluator(genome, dataset)
                candidate_id = stable_hash({"dataset": dataset_hash, "genome": genome.model_dump(), "model": model_version})
                candidates.append(EvolutionCandidate(candidate_id=candidate_id, genome=genome, metrics=metrics, dataset_sha256=dataset_hash, model_version=model_version, created_at=decision_at, decision_at=decision_at))
        passed = [candidate for candidate in candidates if candidate.metrics.sample_count >= self.minimum_samples and candidate.metrics.maximum_drawdown <= self.maximum_drawdown and candidate.metrics.turnover <= self.maximum_turnover]
        champion = max(passed, key=lambda item: (item.metrics.net_return, item.metrics.sharpe, -item.metrics.maximum_drawdown), default=None)
        if champion is not None:
            best_return = champion.metrics.net_return
            champion = champion.model_copy(update={"status": "ACTIVE" if best_return >= self.minimum_improvement else "CANDIDATE"})
            if champion.status != "ACTIVE":
                champion = None
        return EvolutionResult(champion=champion, candidates=tuple(candidates))
