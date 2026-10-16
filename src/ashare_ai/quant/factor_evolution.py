"""Low-frequency, auditable evolution of the deterministic factor genome.

The weekly job asks the configured research model for *parameter* proposals
only.  Every proposal is validated by Pydantic, evaluated with a walk-forward
backtest over frozen historical rows, and — if it clears the gates — stored as
a candidate that an administrator must approve.  Nothing here can change the
active genome, place an order, or write a research result.

The LLM never sees the evaluation data and never returns a score, a target
price, or a trade instruction; it may only move weights and thresholds inside
the bounds enforced by :class:`~ashare_ai.scoring.factor.FactorGenome`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ashare_ai.core.hashing import stable_hash
from ashare_ai.quant.evolution import EvolutionMetrics
from ashare_ai.scoring.factor import (
    DEFAULT_FACTOR_GENOME,
    FactorGenome,
    calculate_factor_score,
    factor_position,
)

# Text the model is forbidden from producing: it may describe parameters and
# hypotheses, never an outcome or an instruction.
_FORBIDDEN_PROPOSAL_PATTERNS = (
    re.compile(r"目标价|target\s*price", re.IGNORECASE),
    re.compile(r"买入|卖出|加仓|减仓|清仓|buy\s+now|sell\s+now", re.IGNORECASE),
    re.compile(r"最终评分|final\s+score|total_score\s*=", re.IGNORECASE),
    re.compile(r"仓位\s*[:=]\s*\d", re.IGNORECASE),
)


class FactorProposal(BaseModel):
    """The only shape an LLM may return for a factor-genome candidate.

    Every field is a *parameter* or an explanation.  There is deliberately no
    field for a score, a price, a position, or an order.
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=1, max_length=2000)
    risk_notes: str = Field(min_length=1, max_length=2000)
    explanation: str = Field(min_length=1, max_length=4000)

    fundamental_weight: float = Field(ge=0.0, le=1.0)
    technical_weight: float = Field(ge=0.0, le=1.0)
    sentiment_weight: float = Field(ge=0.0, le=1.0)
    quality_weight: float = Field(ge=0.0, le=1.0)
    momentum_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    volatility_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    liquidity_weight: float = Field(default=0.0, ge=0.0, le=1.0)

    buy_threshold: float = Field(ge=0.0, le=100.0)
    sell_threshold: float = Field(ge=0.0, le=100.0)
    risk_low_threshold: float = Field(ge=0.0, le=100.0)
    risk_high_threshold: float = Field(ge=0.0, le=100.0)

    max_position: Literal[0, 10, 20, 30, 50, 70, 100] = 70
    risk_budget: float = Field(gt=0.0, le=1.0)

    @field_validator("hypothesis", "risk_notes", "explanation")
    @classmethod
    def reject_outcome_language(cls, value: str) -> str:
        for pattern in _FORBIDDEN_PROPOSAL_PATTERNS:
            if pattern.search(value):
                raise ValueError(
                    "a factor proposal may not contain a final score, a target price, "
                    "or a trade instruction"
                )
        return value

    def to_genome(self, *, base: FactorGenome | None = None) -> FactorGenome:
        """Build a validated genome, inheriting the base's formula version."""

        base = base or DEFAULT_FACTOR_GENOME
        return base.with_parameters(
            fundamental_weight=self.fundamental_weight,
            technical_weight=self.technical_weight,
            sentiment_weight=self.sentiment_weight,
            quality_weight=self.quality_weight,
            momentum_weight=self.momentum_weight,
            volatility_weight=self.volatility_weight,
            liquidity_weight=self.liquidity_weight,
            buy_threshold=self.buy_threshold,
            sell_threshold=self.sell_threshold,
            risk_low_threshold=self.risk_low_threshold,
            risk_high_threshold=self.risk_high_threshold,
            max_position=self.max_position,
            risk_budget=self.risk_budget,
        )

    @property
    def proposal_sha256(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class FactorEvaluationRow(BaseModel):
    """One frozen, point-in-time observation used for walk-forward evaluation."""

    symbol: str
    trading_date: str
    available_at: datetime
    decision_at: datetime
    fundamental_score: float = Field(ge=0, le=100)
    technical_score: float = Field(ge=0, le=100)
    sentiment_score: float = Field(ge=0, le=100)
    quality_confidence_score: float = Field(ge=0, le=100)
    dividend_bonus: float = Field(default=0.0, ge=0, le=10)
    event_risk_multiplier: float = Field(default=1.0, ge=0, le=1)
    market_score_adjustment: float = Field(default=0.0, ge=-10, le=10)
    market_risk_multiplier: float = Field(default=1.0, ge=0.8, le=1)
    # PIT-derived v2 inputs. Missing historical fields use a neutral score so
    # the shadow backtest remains comparable with legacy datasets.
    momentum_score: float = Field(default=50.0, ge=0, le=100)
    volatility_score: float = Field(default=50.0, ge=0, le=100)
    liquidity_score: float = Field(default=50.0, ge=0, le=100)
    forward_return: float = Field(ge=-1.0, le=10.0)

    @model_validator(mode="after")
    def validate_point_in_time(self) -> FactorEvaluationRow:
        if self.available_at > self.decision_at:
            raise ValueError("available_at must not be after decision_at")
        return self


class FactorCandidate(BaseModel):
    """A stored, not-yet-approved factor genome candidate."""

    candidate_id: str
    status: Literal["CANDIDATE", "ACTIVE", "REJECTED", "ROLLED_BACK"] = "CANDIDATE"
    genome: FactorGenome
    metrics: EvolutionMetrics
    dataset_sha256: str
    formula_version: str
    proposal_sha256: str | None = None
    source: Literal["LLM", "DETERMINISTIC"] = "DETERMINISTIC"
    created_at: datetime
    decision_at: datetime


@dataclass(frozen=True)
class FactorEvolutionResult:
    champion: FactorCandidate | None
    candidates: tuple[FactorCandidate, ...]
    rejected: tuple[FactorCandidate, ...]


def split_temporal_rows(
    rows: Sequence[FactorEvaluationRow],
    *,
    train_sessions: int,
    validation_sessions: int,
    test_sessions: int,
    purge_label_sessions: int = 0,
) -> tuple[tuple[FactorEvaluationRow, ...], tuple[FactorEvaluationRow, ...], tuple[FactorEvaluationRow, ...]]:
    """Split frozen observations by unique trading dates in chronological order."""
    ordered = sorted(rows, key=lambda row: (row.trading_date, row.symbol))
    dates = sorted({row.trading_date for row in ordered})
    required = train_sessions + validation_sessions + test_sessions
    if len(dates) < required:
        raise ValueError(f"evolution dataset requires {required} trading sessions, got {len(dates)}")
    dates = dates[-required:]
    train_end = train_sessions - max(0, purge_label_sessions)
    validation_start = train_sessions
    validation_end = train_sessions + validation_sessions - max(0, purge_label_sessions)
    train_dates = set(dates[:max(0, train_end)])
    validation_dates = set(dates[validation_start:max(validation_start, validation_end)])
    test_dates = set(dates[train_sessions + validation_sessions:])
    return (
        tuple(row for row in ordered if row.trading_date in train_dates),
        tuple(row for row in ordered if row.trading_date in validation_dates),
        tuple(row for row in ordered if row.trading_date in test_dates),
    )


def evaluate_factor_genome(
    genome: FactorGenome,
    rows: Sequence[FactorEvaluationRow],
) -> EvolutionMetrics:
    """Walk-forward evaluation of one genome over frozen rows.

    The position implied by the genome is held for the label window and earns
    that row's realized ``forward_return``.  Turnover is the mean absolute
    change in position weight between consecutive rows, expressed in percent.
    """

    if not rows:
        return EvolutionMetrics(
            sample_count=0, net_return=0.0, sharpe=0.0, maximum_drawdown=0.0, turnover=0.0
        )

    equity = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    returns: list[float] = []
    previous_weights: dict[str, float] = {}
    turnover_total = 0.0

    for row in rows:
        score = calculate_factor_score(
            fundamental_score=row.fundamental_score,
            technical_score=row.technical_score,
            sentiment_score=row.sentiment_score,
            quality_confidence_score=row.quality_confidence_score,
            genome=genome,
            dividend_bonus=row.dividend_bonus,
            event_risk_multiplier=row.event_risk_multiplier,
            market_score_adjustment=row.market_score_adjustment,
            market_risk_multiplier=row.market_risk_multiplier,
            momentum_score=row.momentum_score,
            volatility_score=row.volatility_score,
            liquidity_score=row.liquidity_score,
        )
        weight = factor_position(score, genome) / 100.0
        previous_weight = previous_weights.get(row.symbol, 0.0)
        turnover_total += abs(weight - previous_weight)
        previous_weights[row.symbol] = weight

        period_return = weight * row.forward_return
        returns.append(period_return)
        equity *= 1.0 + period_return
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak if peak else 0.0)

    mean_return = sum(returns) / len(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / len(returns)
    deviation = variance**0.5
    sharpe = mean_return / deviation if deviation > 1e-12 else 0.0

    return EvolutionMetrics(
        sample_count=len(rows),
        net_return=round(equity - 1.0, 6),
        sharpe=round(sharpe, 6),
        maximum_drawdown=round(maximum_drawdown, 6),
        turnover=round(turnover_total / len(rows) * 100.0, 6),
    )


class FactorEvolutionService:
    """Evaluate factor genomes against the same gates as the generic service."""

    def __init__(
        self,
        *,
        minimum_samples: int = 60,
        maximum_drawdown: float = 0.20,
        minimum_improvement: float = 0.0,
        maximum_turnover: float = 20.0,
        maximum_candidates: int = 8,
    ):
        self.minimum_samples = minimum_samples
        self.maximum_drawdown = maximum_drawdown
        self.minimum_improvement = minimum_improvement
        self.maximum_turnover = maximum_turnover
        self.maximum_candidates = maximum_candidates

    @staticmethod
    def validate_point_in_time(
        rows: Sequence[FactorEvaluationRow], decision_at: datetime
    ) -> None:
        """Reject any row whose evidence was not available at ``decision_at``."""

        for row in rows:
            if row.available_at > decision_at:
                raise ValueError("evolution dataset contains evidence after decision_at")
            if row.decision_at > decision_at:
                raise ValueError("evolution dataset contains a decision after decision_at")

    @staticmethod
    def deterministic_candidates(
        baseline: FactorGenome, *, limit: int
    ) -> list[FactorGenome]:
        """Deterministic parameter search used when no LLM is available.

        The walk is a pure function of the baseline genome, so a run is
        reproducible and never depends on global RNG state.
        """

        candidates: list[FactorGenome] = []
        for index in range(max(0, limit)):
            value = (baseline.mutation_seed * 1103515245 + (index + 1) * 12345) & 0x7FFFFFFF
            weight_delta = ((value % 2001) - 1000) / 20000.0
            threshold_delta = ((value // 2001) % 2001 - 1000) / 100.0
            try:
                candidates.append(
                    baseline.with_parameters(
                        fundamental_weight=baseline.fundamental_weight + weight_delta,
                        technical_weight=baseline.technical_weight - weight_delta,
                        buy_threshold=baseline.buy_threshold + threshold_delta,
                        sell_threshold=baseline.sell_threshold + threshold_delta / 2.0,
                        mutation_seed=value,
                    )
                )
            except ValueError:
                # A proposal that leaves the safe bounds is simply skipped.
                continue
        return candidates

    def evaluate(
        self,
        *,
        rows: Sequence[FactorEvaluationRow],
        baseline: FactorGenome,
        decision_at: datetime,
        proposals: Sequence[FactorProposal] = (),
        dataset_sha256: str | None = None,
        training_rows: Sequence[FactorEvaluationRow] | None = None,
        validation_rows: Sequence[FactorEvaluationRow] | None = None,
    ) -> FactorEvolutionResult:
        """Evaluate the baseline plus every proposal and apply the gates."""

        self.validate_point_in_time(rows, decision_at)
        dataset_hash = dataset_sha256 or stable_hash(
            [row.model_dump(mode="json") for row in rows]
        )

        baseline_metrics = evaluate_factor_genome(baseline, rows)
        training_baseline = evaluate_factor_genome(baseline, training_rows) if training_rows is not None else baseline_metrics
        validation_baseline = evaluate_factor_genome(baseline, validation_rows) if validation_rows is not None else None
        scored: list[tuple[FactorGenome, EvolutionMetrics, FactorProposal | None]] = []
        for proposal in proposals[: self.maximum_candidates]:
            try:
                genome = proposal.to_genome(base=baseline)
            except ValueError:
                continue
            scored.append((genome, evaluate_factor_genome(genome, rows), proposal))

        candidates: list[FactorCandidate] = []
        rejected: list[FactorCandidate] = []
        for genome, metrics, proposal in scored:
            candidate = FactorCandidate(
                candidate_id=stable_hash(
                    {
                        "dataset": dataset_hash,
                        "genome": genome.model_dump(mode="json"),
                        "formula": genome.formula_version,
                    }
                ),
                genome=genome,
                metrics=metrics,
                dataset_sha256=dataset_hash,
                formula_version=genome.formula_version,
                proposal_sha256=proposal.proposal_sha256 if proposal else None,
                source="LLM" if proposal else "DETERMINISTIC",
                created_at=decision_at,
                decision_at=decision_at,
            )
            passes = self._passes_gates(candidate, baseline_metrics)
            if training_rows is not None:
                passes = passes and self._passes_gates(
                    candidate.model_copy(update={"metrics": evaluate_factor_genome(genome, training_rows)}),
                    training_baseline,
                )
            if validation_rows is not None and validation_baseline is not None:
                validation_metrics = evaluate_factor_genome(genome, validation_rows)
                passes = passes and (
                    validation_metrics.maximum_drawdown <= self.maximum_drawdown
                    and validation_metrics.turnover <= self.maximum_turnover
                    and validation_metrics.net_return - validation_baseline.net_return
                    >= self.minimum_improvement
                )
            if passes:
                candidates.append(candidate)
            else:
                rejected.append(candidate.model_copy(update={"status": "REJECTED"}))

        champion = max(
            candidates,
            key=lambda item: (
                item.metrics.net_return,
                item.metrics.sharpe,
                -item.metrics.maximum_drawdown,
            ),
            default=None,
        )
        return FactorEvolutionResult(
            champion=champion,
            candidates=tuple(candidates),
            rejected=tuple(rejected),
        )

    def _passes_gates(
        self, candidate: FactorCandidate, baseline: EvolutionMetrics
    ) -> bool:
        metrics = candidate.metrics
        if metrics.sample_count < self.minimum_samples:
            return False
        if metrics.maximum_drawdown > self.maximum_drawdown:
            return False
        if metrics.turnover > self.maximum_turnover:
            return False
        return metrics.net_return - baseline.net_return >= self.minimum_improvement


def build_proposal_prompt(baseline: FactorGenome, metrics: EvolutionMetrics) -> list[dict[str, str]]:
    """Prompt for one bounded parameter proposal.

    The model receives the current parameters and aggregate metrics only — no
    symbols, no dates, and no per-row data.
    """

    return [
        {
            "role": "system",
            "content": (
                "你是A股因子权重研究员。只输出因子权重和阈值参数，"
                "不得输出最终评分、目标价、买卖指令或仓位建议。"
                "权重必须归一化到 1，阈值必须保持 buy > sell 且 risk_low > risk_high。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前参数: {baseline.model_dump_json()}\n"
                f"当前回测指标: {metrics.model_dump_json()}\n"
                "请给出一个候选参数组合，并说明实验假设、风险和解释。"
            ),
        },
    ]


def proposal_from_llm_output(output: Any) -> FactorProposal:
    """Validate raw LLM output into a proposal, raising on any violation."""

    return FactorProposal.model_validate(output)
