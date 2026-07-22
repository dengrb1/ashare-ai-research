from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from ashare_ai.core.config import get_settings
from ashare_ai.core.security import safe_error_message
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.orchestration.daily import Pipeline, load_pipeline
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import JobRun

QUEUE_NAME = "ashare:research:pending"
PROCESSING_QUEUE_NAME = "ashare:research:processing"
DELAYED_QUEUE_NAME = "ashare:research:delayed"

_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "FUSED", "CANCELLED"}


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


def enqueue_research_at(
    run_id: str, available_at: datetime, redis_url: str | None = None
) -> None:
    import redis

    client = redis.Redis.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        delayed=DELAYED_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    ).enqueue_at(run_id, available_at.timestamp())


def _retry_waiting_research(run_id: str) -> bool:
    """Promote a due data-readiness wait, or persist its next safe retry.

    The immutable run parameters live in the manifest; only operational wait
    metadata is advanced here.
    """
    from ashare_ai.orchestration.research_schedule import AKShareDataReadiness

    now = datetime.now(UTC)
    with SessionLocal() as session:
        run = session.get(JobRun, run_id)
        if run is None:
            return False
        if run.status != "DATA_READINESS_WAITING":
            return True
        wait = dict((run.manifest or {}).get("data_readiness_wait") or {})
        deadline_raw = wait.get("deadline_at")
        try:
            deadline = datetime.fromisoformat(str(deadline_raw))
            deadline = deadline if deadline.tzinfo else deadline.replace(tzinfo=UTC)
        except ValueError:
            deadline = now
        if now >= deadline:
            run.status = "FAILED"
            run.active_research_key = None
            run.error_message = "benchmark data did not synchronize before the next trading session"
            run.completed_at = now
            AuditLogger(session).record(
                run_id, "DATA_READINESS_TIMEOUT", "Benchmark data wait expired",
                severity="ERROR", details={"deadline_at": deadline.isoformat()},
            )
            session.commit()
            return False
        ready = False
        try:
            ready = AKShareDataReadiness().ready(run.trading_date, now)
        except Exception:
            ready = False
        if ready:
            run.status = "PENDING"
            wait["next_retry_at"] = None
            run.manifest = {**dict(run.manifest), "data_readiness_wait": wait}
            AuditLogger(session).record(
                run_id, "DATA_READINESS_READY", "Required benchmark data synchronized",
                details={"trading_date": run.trading_date.isoformat()},
            )
            session.commit()
            return True
        retry_at = min(
            now + timedelta(minutes=get_settings().daily_research_retry_minutes), deadline
        )
        wait["next_retry_at"] = retry_at.isoformat()
        wait["attempt_count"] = int(wait.get("attempt_count", 0)) + 1
        run.manifest = {**dict(run.manifest), "data_readiness_wait": wait}
        AuditLogger(session).record(
            run_id, "DATA_READINESS_RETRY", "Required benchmark data is not synchronized yet",
            details={"next_retry_at": retry_at.isoformat(), "attempt_count": wait["attempt_count"]},
        )
        session.commit()
    enqueue_research_at(run_id, retry_at)
    return False


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
        if run.status == "CANCEL_REQUESTED":
            _complete_cancellation(session, run, boundary="stage_failed")
            session.commit()
            return
        if run.status == "CANCELLED":
            return
        run.status = "FAILED"
        run.active_research_key = None
        run.error_message = safe_error_message(error)
        run.completed_at = datetime.now(UTC)
        AuditLogger(session).record(
            run_id,
            "RESEARCH_FAILED",
            "Daily research execution failed",
            severity="ERROR",
            details=_error_details(error),
        )
        session.commit()


def _complete_cancellation(session: Session, run: JobRun, *, boundary: str) -> None:
    if run.status == "CANCELLED":
        return
    run.status = "CANCELLED"
    run.active_research_key = None
    run.error_message = None
    run.completed_at = datetime.now(UTC)
    AuditLogger(session).record(
        run.run_id,
        "RESEARCH_CANCELLED",
        "Daily research stopped at a pipeline stage boundary",
        details={"boundary": boundary},
    )


def _stop_if_cancelled(
    run_id: str,
    *,
    boundary: str,
    session_factory: Callable[[], Session],
) -> dict[str, Any] | None:
    with session_factory() as session:
        run = session.get(JobRun, run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status == "CANCEL_REQUESTED":
            _complete_cancellation(session, run, boundary=boundary)
            session.commit()
            return {"run_id": run_id, "status": "CANCELLED"}
        if run.status == "CANCELLED":
            return {"run_id": run_id, "status": "CANCELLED"}
    return None


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
        if run.status in _TERMINAL_STATUSES:
            return {"run_id": run_id, "status": run.status}
        if run.status == "CANCEL_REQUESTED":
            _complete_cancellation(session, run, boundary="before_claim")
            session.commit()
            return {"run_id": run_id, "status": "CANCELLED"}
        if run.status in {"PENDING", "QUEUED"}:
            session.execute(
                update(JobRun)
                .where(
                    JobRun.run_id == run_id,
                    JobRun.status.in_(("PENDING", "QUEUED")),
                )
                .values(status="RUNNING", error_message=None)
            )
            session.commit()
            session.refresh(run)
            if run.status in {"CANCEL_REQUESTED", "CANCELLED"}:
                if run.status == "CANCEL_REQUESTED":
                    _complete_cancellation(session, run, boundary="before_claim")
                    session.commit()
                return {"run_id": run_id, "status": "CANCELLED"}
            if run.status in _TERMINAL_STATUSES:
                return {"run_id": run_id, "status": run.status}
        elif run.status not in {"RUNNING", "PROCESSING"}:
            raise RuntimeError(f"research run cannot be claimed from status {run.status}")
        AuditLogger(session).record(
            run_id,
            "RESEARCH_STARTED",
            "Daily research worker started the frozen run",
            details={"trading_date": run.trading_date.isoformat()},
        )
        session.commit()
    try:
        pipeline.sync_reference_data(run_id)
        if result := _stop_if_cancelled(
            run_id, boundary="sync_reference_data", session_factory=session_factory
        ):
            return result
        snapshots = pipeline.ingest_and_verify(run_id)
        if result := _stop_if_cancelled(
            run_id, boundary="ingest_and_verify", session_factory=session_factory
        ):
            return result
        universe_id = pipeline.build_universe(run_id, snapshots)
        if result := _stop_if_cancelled(
            run_id, boundary="build_universe", session_factory=session_factory
        ):
            return result
        feature_snapshot_id = pipeline.build_features(run_id, universe_id)
        if result := _stop_if_cancelled(
            run_id, boundary="build_features", session_factory=session_factory
        ):
            return result
        agent_bundle_id = pipeline.run_research_agents(run_id, feature_snapshot_id)
        if result := _stop_if_cancelled(
            run_id, boundary="run_research_agents", session_factory=session_factory
        ):
            return result
        score_snapshot_id = pipeline.calculate_scores(run_id, agent_bundle_id)
        if result := _stop_if_cancelled(
            run_id, boundary="calculate_scores", session_factory=session_factory
        ):
            return result
        candidate_snapshot_id = pipeline.qlib_filter(run_id, score_snapshot_id)
        if result := _stop_if_cancelled(
            run_id, boundary="qlib_filter", session_factory=session_factory
        ):
            return result
        risk_state = pipeline.risk_state(run_id)
        if result := _stop_if_cancelled(
            run_id, boundary="risk_state", session_factory=session_factory
        ):
            return result
        portfolio_id = None
        final_status = "FUSED" if risk_state == "OBSERVE_ONLY" else "SUCCEEDED"
        portfolio_requested = getattr(pipeline, "portfolio_requested", lambda _: True)(run_id)
        if risk_state != "OBSERVE_ONLY" and portfolio_requested:
            portfolio_id = pipeline.build_portfolio(run_id, candidate_snapshot_id)
            if result := _stop_if_cancelled(
                run_id, boundary="build_portfolio", session_factory=session_factory
            ):
                return result
        report_id = pipeline.publish_report(run_id, portfolio_id, risk_state)
        if result := _stop_if_cancelled(
            run_id, boundary="publish_report", session_factory=session_factory
        ):
            return result
        result = pipeline.complete_run(run_id, report_id, final_status)
        with session_factory() as session:
            completed = session.get(JobRun, run_id)
            if completed is not None:
                completed.active_research_key = None
                session.commit()
        return result
    except Exception as exc:
        # Bundle construction performs the same complete-benchmark check as the
        # submission probe. A provider race is therefore deferred, never
        # converted into an opaque terminal snapshot failure.
        from ashare_ai.orchestration.akshare_bundle import BenchmarkDataNotReadyError

        if isinstance(exc, BenchmarkDataNotReadyError):
            now = datetime.now(UTC)
            with session_factory() as session:
                run = session.get(JobRun, run_id)
                if run is not None:
                    wait = dict((run.manifest or {}).get("data_readiness_wait") or {})
                    deadline_raw = wait.get("deadline_at")
                    deadline = (
                        datetime.fromisoformat(str(deadline_raw))
                        if deadline_raw
                        else now
                        + timedelta(minutes=get_settings().daily_research_retry_limit_minutes)
                    )
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=UTC)
                    if now < deadline:
                        retry_at = min(
                            now + timedelta(minutes=get_settings().daily_research_retry_minutes),
                            deadline,
                        )
                        wait.update(
                            {
                                "deadline_at": deadline.isoformat(),
                                "next_retry_at": retry_at.isoformat(),
                                "attempt_count": int(wait.get("attempt_count", 0)) + 1,
                            }
                        )
                        run.status = "DATA_READINESS_WAITING"
                        run.error_message = None
                        run.completed_at = None
                        run.manifest = {**dict(run.manifest), "data_readiness_wait": wait}
                        AuditLogger(session).record(
                            run_id,
                            "DATA_READINESS_WAITING",
                            "Research paused until all required benchmarks synchronize",
                            details={**_error_details(exc), "next_retry_at": retry_at.isoformat()},
                        )
                        session.commit()
                        enqueue_research_at(run_id, retry_at)
                        return {"run_id": run_id, "status": "DATA_READINESS_WAITING"}
        mark_research_failed(run_id, exc, session_factory=session_factory)
        raise


def run_research_job(run_id: str) -> dict[str, Any]:
    if not _retry_waiting_research(run_id):
        return {"run_id": run_id, "status": "DATA_READINESS_WAITING"}
    return execute_research_job(run_id, pipeline=load_pipeline())


def consume_research_queue() -> None:
    import redis

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    queue = RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        delayed=DELAYED_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )
    queue.consume_forever(run_research_job)
