from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ashare_ai.core.config import get_settings
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.orchestration.daily import Pipeline, load_pipeline
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import JobRun

QUEUE_NAME = "ashare:research:pending"
PROCESSING_QUEUE_NAME = "ashare:research:processing"


def _error_details(error: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {"error_type": type(error).__name__}
    audit_details = getattr(error, "audit_details", None)
    if callable(audit_details):
        value = audit_details()
        if isinstance(value, dict):
            details.update(value)
    return details


def enqueue_research(run_id: str, redis_url: str | None = None) -> None:
    import redis

    client = redis.Redis.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    ).enqueue(run_id)


def mark_research_failed(
    run_id: str,
    error: Exception,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as session:
        run = session.get(JobRun, run_id)
        if run is None:
            return
        run.status = "FAILED"
        run.active_research_key = None
        run.error_message = str(error)
        run.completed_at = datetime.now(UTC)
        AuditLogger(session).record(
            run_id,
            "RESEARCH_FAILED",
            "Daily research execution failed",
            severity="ERROR",
            details=_error_details(error),
        )
        session.commit()


def execute_research_job(
    run_id: str,
    *,
    pipeline: Pipeline,
    session_factory: Callable[[], Session] = SessionLocal,
) -> dict[str, Any]:
    with session_factory() as session:
        run = session.get(JobRun, run_id)
        if run is None or run.run_type != "DAILY":
            raise KeyError(run_id)
        if run.status in {"SUCCEEDED", "FUSED"}:
            return {"run_id": run_id, "status": run.status}
        run.status = "RUNNING"
        run.error_message = None
        AuditLogger(session).record(
            run_id,
            "RESEARCH_STARTED",
            "Daily research worker started the frozen run",
            details={"trading_date": run.trading_date.isoformat()},
        )
        session.commit()
    try:
        pipeline.sync_reference_data(run_id)
        snapshots = pipeline.ingest_and_verify(run_id)
        universe_id = pipeline.build_universe(run_id, snapshots)
        feature_snapshot_id = pipeline.build_features(run_id, universe_id)
        agent_bundle_id = pipeline.run_research_agents(run_id, feature_snapshot_id)
        score_snapshot_id = pipeline.calculate_scores(run_id, agent_bundle_id)
        candidate_snapshot_id = pipeline.qlib_filter(run_id, score_snapshot_id)
        risk_state = pipeline.risk_state(run_id)
        portfolio_id = None
        final_status = "FUSED" if risk_state == "OBSERVE_ONLY" else "SUCCEEDED"
        if risk_state != "OBSERVE_ONLY":
            portfolio_id = pipeline.build_portfolio(run_id, candidate_snapshot_id)
        report_id = pipeline.publish_report(run_id, portfolio_id, risk_state)
        result = pipeline.complete_run(run_id, report_id, final_status)
        with session_factory() as session:
            completed = session.get(JobRun, run_id)
            if completed is not None:
                completed.active_research_key = None
                session.commit()
        return result
    except Exception as exc:
        mark_research_failed(run_id, exc, session_factory=session_factory)
        raise


def run_research_job(run_id: str) -> dict[str, Any]:
    return execute_research_job(run_id, pipeline=load_pipeline())


def consume_research_queue() -> None:
    import redis

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    queue = RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )
    queue.consume_forever(run_research_job)
