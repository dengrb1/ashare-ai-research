"""Weekly, low-frequency factor-genome evolution job.

The job runs on the existing single serial worker.  It builds a point-in-time
dataset from frozen scores and frozen bundles, evaluates the active genome plus
a bounded set of candidates, and stores everything that clears the gates as a
*candidate* awaiting administrator approval.  It never changes the active
genome, never writes a research result, and never places an order.

The research model is called at most once per run and only ever returns
parameters and prose; deterministic parameter search is the fallback whenever
the model is unavailable or its output fails validation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.core.system_settings import get_effective_settings
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.quant.factor_evolution import (
    FactorEvaluationRow,
    FactorEvolutionService,
    FactorProposal,
    build_proposal_prompt,
    evaluate_factor_genome,
    proposal_from_llm_output,
    split_temporal_rows,
)
from ashare_ai.quant.factor_registry import FactorGenomeRegistry
from ashare_ai.scoring.factor import FactorGenome

logger = logging.getLogger(__name__)

QUEUE_NAME = "ashare:strategy-evolution:pending"
PROCESSING_QUEUE_NAME = "ashare:strategy-evolution:processing"
PAYLOAD_PREFIX = "ashare:strategy-evolution:payload:"
STATUS_PREFIX = "ashare:strategy-evolution:status:"
RETENTION_SECONDS = 30 * 24 * 60 * 60

# Fixed walk-forward windows.  The label window is deliberately short so the
# dataset stays inside the frozen bundle horizon.
TRAIN_SESSIONS = 120
VALIDATION_SESSIONS = 40
TEST_SESSIONS = 40
LABEL_SESSIONS = 5


def _keys(job_id: str) -> tuple[str, str]:
    return f"{PAYLOAD_PREFIX}{job_id}", f"{STATUS_PREFIX}{job_id}"


def _queue(client: Any) -> RedisLeasedQueue:
    return RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )


def weekly_job_id(now: datetime) -> str:
    """One idempotent job per ISO week, so a retry cannot double-run."""

    iso = now.isocalendar()
    return stable_hash({"kind": "strategy-evolution-v1", "year": iso.year, "week": iso.week})


def enqueue_strategy_evolution(
    *,
    now: datetime | None = None,
    redis_url: str | None = None,
) -> str:
    """Persist and enqueue this week's evolution run (idempotent)."""

    import redis

    moment = now or datetime.now(UTC)
    job_id = weekly_job_id(moment)
    payload = {"requested_at": moment.isoformat(), "week": job_id}
    payload_key, status_key = _keys(job_id)
    client = redis.Redis.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    if client.exists(status_key):
        return job_id
    client.hset(payload_key, mapping={"payload": json.dumps(payload, ensure_ascii=False)})
    client.expire(payload_key, RETENTION_SECONDS)
    client.hset(
        status_key,
        mapping={"status": "QUEUED", "created_at": moment.isoformat(), "job_id": job_id},
    )
    client.expire(status_key, RETENTION_SECONDS)
    _queue(client).enqueue(job_id)
    return job_id


def _set_status(client: Any, job_id: str, **values: object) -> None:
    _, status_key = _keys(job_id)
    client.hset(status_key, mapping={key: str(value) for key, value in values.items()})
    client.expire(status_key, RETENTION_SECONDS)


def get_strategy_evolution_status(client: Any, job_id: str) -> dict[str, str] | None:
    _, status_key = _keys(job_id)
    values = client.hgetall(status_key)
    return dict(values) if values else None


def dispatch_weekly_evolution(*, now: datetime | None = None) -> dict[str, Any]:
    """Enqueue the weekly run when the schedule says it is due.

    Called from the lightweight scheduler tick.  It only enqueues; the actual
    work happens in the isolated worker process.
    """

    from ashare_ai.orchestration.runner import SHANGHAI

    moment = now or datetime.now(SHANGHAI)
    config = get_effective_settings()
    if not config.strategy_evolution_enabled:
        return {"enqueued": False, "reason": "DISABLED"}
    if moment.weekday() != config.strategy_evolution_weekday:
        return {"enqueued": False, "reason": "NOT_SCHEDULED_DAY"}
    if moment.hour != config.strategy_evolution_hour:
        return {"enqueued": False, "reason": "NOT_SCHEDULED_HOUR"}
    try:
        job_id = enqueue_strategy_evolution(now=moment)
    except Exception as exc:  # pragma: no cover - redis unavailable
        logger.warning("strategy evolution enqueue failed: %s", exc)
        return {"enqueued": False, "reason": "ENQUEUE_FAILED"}
    return {"enqueued": True, "job_id": job_id}


# --------------------------------------------------------------------- dataset


def _forward_returns(
    bundle_dir: Path, trading_dates: list[date]
) -> dict[tuple[str, date], float]:
    """Realized close-to-close returns from frozen bundles only.

    The label is the outcome the position would have earned; it is read from
    frozen bundles, never from a live quote.
    """

    from ashare_ai.orchestration.bundle_loader import load_bundle_from_disk

    closes: dict[date, dict[str, float]] = {}
    for trading_date in trading_dates:
        try:
            bundle = load_bundle_from_disk(bundle_dir, trading_date)
        except (FileNotFoundError, ValueError):
            continue
        if any(bar.available_at > bundle.decision_at for bar in bundle.bars):
            continue
        latest: dict[str, Any] = {}
        for bar in bundle.bars:
            prior = latest.get(bar.symbol)
            if prior is None or bar.available_at > prior.available_at:
                latest[bar.symbol] = bar
        closes[trading_date] = {symbol: float(bar.close) for symbol, bar in latest.items()}

    ordered = sorted(closes)
    returns: dict[tuple[str, date], float] = {}
    for index, trading_date in enumerate(ordered):
        label_index = index + LABEL_SESSIONS
        if label_index >= len(ordered):
            break
        label_date = ordered[label_index]
        for symbol, close in closes[trading_date].items():
            future = closes[label_date].get(symbol)
            if not future or close <= 0:
                continue
            returns[(symbol, trading_date)] = future / close - 1.0
    return returns


def build_evaluation_dataset(
    *,
    decision_at: datetime,
    sessions: int = TRAIN_SESSIONS + VALIDATION_SESSIONS + TEST_SESSIONS,
    bundle_dir: Path | None = None,
) -> list[FactorEvaluationRow]:
    """Build frozen, point-in-time rows for the walk-forward evaluation.

    Only scores whose ``decision_at`` is at or before the job's ``decision_at``
    are read, and every row's ``available_at`` is its own decision time, so the
    dataset cannot contain future evidence.
    """

    from ashare_ai.storage.database import SessionLocal
    from ashare_ai.storage.models import ScoreRow

    directory = bundle_dir or get_effective_settings().bundle_storage_dir
    with SessionLocal() as session:
        rows = session.scalars(
            select(ScoreRow)
            .where(ScoreRow.decision_at <= decision_at)
            .order_by(ScoreRow.trading_date.desc(), ScoreRow.symbol)
            .limit(max(1, sessions) * 200)
        ).all()
        if not rows:
            return []
        trading_dates = sorted({row.trading_date for row in rows})
        trading_dates = trading_dates[-max(1, sessions):]
        returns = _forward_returns(directory, trading_dates)
        dataset: list[FactorEvaluationRow] = []
        for row in rows:
            if row.trading_date not in trading_dates:
                continue
            forward = returns.get((row.symbol, row.trading_date))
            if forward is None:
                continue
            dataset.append(
                FactorEvaluationRow(
                    symbol=row.symbol,
                    trading_date=row.trading_date.isoformat(),
                    available_at=row.decision_at,
                    decision_at=row.decision_at,
                    fundamental_score=row.fundamental_score,
                    technical_score=row.technical_score,
                    sentiment_score=row.sentiment_score,
                    quality_confidence_score=row.quality_confidence_score,
                    dividend_bonus=row.dividend_bonus,
                    event_risk_multiplier=row.event_risk_multiplier,
                    market_score_adjustment=row.market_score_adjustment,
                    market_risk_multiplier=row.market_risk_multiplier,
                    forward_return=max(-1.0, min(10.0, forward)),
                )
            )
    dataset.sort(key=lambda item: (item.trading_date, item.symbol))
    return dataset


# ------------------------------------------------------------------- LLM call


async def _request_proposal(
    baseline: FactorGenome, metrics: Any
) -> FactorProposal | None:
    """Ask the research model once for one bounded parameter proposal.

    Returns ``None`` when no model is configured or the output fails
    validation; the caller then falls back to deterministic search.
    """

    from ashare_ai.agents.model_settings import ModelConfigurationService
    from ashare_ai.storage.database import SessionLocal

    settings = get_effective_settings()
    with SessionLocal() as session:
        runtime = ModelConfigurationService(settings).resolve(session)
    if runtime is None or not runtime.enabled:
        return None

    client = ModelConfigurationService(settings).client_for(
        runtime,
        purpose="strategy",
        timeout_seconds=min(runtime.timeout_seconds, settings.llm_timeout_seconds),
        max_retries=1,
    )
    try:
        generation = await client.generate_structured(
            schema=FactorProposal,
            messages=tuple(build_proposal_prompt(baseline, metrics)),
            idempotency_key=stable_hash(
                {"system": "factor-proposal-v1", "genome": baseline, "metrics": metrics}
            ),
        )
        return proposal_from_llm_output(generation.output)
    except Exception as exc:
        logger.warning("factor proposal rejected: %s", exc)
        return None
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()


# ----------------------------------------------------------------------- job


def run_strategy_evolution_job(job_id: str) -> None:
    """Run one queued evolution pass in the isolated worker process."""

    import redis

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    payload_key, _ = _keys(job_id)
    raw = client.hget(payload_key, "payload")
    if not raw:
        _set_status(client, job_id, status="FAILED", error="evolution payload is missing")
        return
    started_at = datetime.now(UTC)
    _set_status(client, job_id, status="RUNNING", started_at=started_at.isoformat())
    try:
        payload = json.loads(raw)
        requested_at = datetime.fromisoformat(payload["requested_at"])
        decision_at = requested_at.astimezone(UTC)

        from ashare_ai.storage.database import SessionLocal

        with SessionLocal() as session:
            registry = FactorGenomeRegistry(session)
            baseline = registry.active_genome()
            dataset = build_evaluation_dataset(decision_at=decision_at)
            if not dataset:
                _set_status(
                    client,
                    job_id,
                    status="SUCCEEDED",
                    completed_at=datetime.now(UTC).isoformat(),
                    candidates=0,
                    reason="NO_DATASET",
                )
                return

            config = get_effective_settings()
            service = FactorEvolutionService(
                minimum_samples=config.strategy_evolution_min_samples,
                maximum_drawdown=config.strategy_evolution_max_drawdown,
                maximum_turnover=config.strategy_evolution_max_turnover,
                minimum_improvement=config.strategy_evolution_min_improvement,
                maximum_candidates=config.strategy_evolution_max_candidates,
            )

            # At most one model call per run; deterministic search always runs.
            proposals: list[FactorProposal] = []
            proposal = asyncio.run(
                _request_proposal(baseline, evaluate_factor_genome(baseline, dataset))
            )
            if proposal is not None:
                proposals.append(proposal)

            deterministic = service.deterministic_candidates(
                baseline, limit=config.strategy_evolution_max_candidates
            )
            for genome in deterministic:
                proposals.append(
                    FactorProposal(
                        hypothesis="deterministic parameter walk",
                        risk_notes="bounded weight and threshold perturbation",
                        explanation="generated without a model call",
                        fundamental_weight=genome.fundamental_weight,
                        technical_weight=genome.technical_weight,
                        sentiment_weight=genome.sentiment_weight,
                        quality_weight=genome.quality_weight,
                        buy_threshold=genome.buy_threshold,
                        sell_threshold=genome.sell_threshold,
                        risk_low_threshold=genome.risk_low_threshold,
                        risk_high_threshold=genome.risk_high_threshold,
                        max_position=genome.max_position,
                        risk_budget=genome.risk_budget,
                    )
                )

            train_rows, validation_rows, test_rows = split_temporal_rows(
                dataset,
                train_sessions=TRAIN_SESSIONS,
                validation_sessions=VALIDATION_SESSIONS,
                test_sessions=TEST_SESSIONS,
                purge_label_sessions=LABEL_SESSIONS,
            )
            evaluation_rows = train_rows + validation_rows
            result = service.evaluate(
                rows=evaluation_rows,
                baseline=baseline,
                decision_at=decision_at,
                proposals=proposals[: config.strategy_evolution_max_candidates],
                dataset_sha256=stable_hash([row.model_dump(mode="json") for row in dataset]),
                training_rows=train_rows,
                validation_rows=validation_rows,
            )
            by_hash = {item.proposal_sha256: item for item in proposals}
            stored = registry.store_candidates(
                result.candidates,
                proposals={
                    candidate.candidate_id: by_hash[candidate.proposal_sha256]
                    for candidate in result.candidates
                    if candidate.proposal_sha256 in by_hash
                },
            )
            session.commit()

        _set_status(
            client,
            job_id,
            status="SUCCEEDED",
            completed_at=datetime.now(UTC).isoformat(),
            samples=len(dataset),
            candidates=len(stored),
            rejected=len(result.rejected),
            champion=result.champion.candidate_id if result.champion else "",
            train_samples=len(train_rows),
            validation_samples=len(validation_rows),
            test_samples=len(test_rows),
            test_baseline_net_return=evaluate_factor_genome(baseline, test_rows).net_return,
        )
    except Exception as exc:
        _set_status(
            client,
            job_id,
            status="FAILED",
            completed_at=datetime.now(UTC).isoformat(),
            error=type(exc).__name__,
        )
        raise
