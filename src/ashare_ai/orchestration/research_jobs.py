from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from ashare_ai.core.config import get_settings
from ashare_ai.core.security import safe_error_message
from ashare_ai.core.system_settings import (
    SystemConfigurationService,
    SystemRuntimeSettings,
    get_effective_settings,
)
from ashare_ai.notifications.service import NotificationService
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.orchestration.daily import Pipeline, load_pipeline
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.orchestration.worker_status import publish_heartbeat
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import JobRun

QUEUE_NAME = "ashare:research:pending"
PROCESSING_QUEUE_NAME = "ashare:research:processing"
DELAYED_QUEUE_NAME = "ashare:research:delayed"

_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "FUSED", "CANCELLED"}
logger = logging.getLogger(__name__)


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
        lease_seconds=get_effective_settings().worker_lease_seconds,
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
        lease_seconds=get_effective_settings().worker_lease_seconds,
    ).enqueue_at(run_id, available_at.timestamp())


def _retry_waiting_research(
    run_id: str,
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    readiness: Callable[[date, datetime], bool] | None = None,
    enqueue_at: Callable[[str, datetime], None] = enqueue_research_at,
) -> bool:
    """Promote a due data-readiness wait, or persist its next safe retry.

    The immutable run parameters live in the manifest; only operational wait
    metadata is advanced here.
    """
    from ashare_ai.orchestration.research_schedule import AKShareDataReadiness

    current = now or datetime.now(UTC)
    with session_factory() as session:
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
            deadline = current
        ready = False
        try:
            ready = (
                readiness(run.trading_date, current)
                if readiness is not None
                else AKShareDataReadiness().ready(run.trading_date, current)
            )
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
        if current >= deadline:
            # The 09:25 boundary means "not ready by then", not "skip the
            # last readiness probe".  A feed that arrives exactly at the
            # deadline remains safe because the next continuous session has
            # not begun yet.
            run.status = "FAILED"
            run.active_research_key = None
            run.error_message = "benchmark data did not synchronize before the next trading session"
            run.completed_at = current
            AuditLogger(session).record(
                run_id, "DATA_READINESS_TIMEOUT", "Benchmark data wait expired",
                severity="ERROR", details={"deadline_at": deadline.isoformat()},
            )
            session.commit()
            return False
        retry_at = min(
            current
            + timedelta(minutes=get_effective_settings().daily_research_retry_minutes),
            deadline,
        )
        wait["next_retry_at"] = retry_at.isoformat()
        wait["attempt_count"] = int(wait.get("attempt_count", 0)) + 1
        run.manifest = {**dict(run.manifest), "data_readiness_wait": wait}
        AuditLogger(session).record(
            run_id, "DATA_READINESS_RETRY", "Required benchmark data is not synchronized yet",
            details={"next_retry_at": retry_at.isoformat(), "attempt_count": wait["attempt_count"]},
        )
        session.commit()
    enqueue_at(run_id, retry_at)
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
        if run.user_id:
            NotificationService(session).create(
                user_id=run.user_id,
                notification_type="RESEARCH_FAILED",
                severity="WARNING",
                title="正式研究运行失败",
                body="本次研究未完成；没有生成或执行自动交易。",
                resource_type="RESEARCH_RUN",
                resource_id=run.run_id,
                dedupe_key=f"research-failed:{run.run_id}",
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
                if completed.user_id:
                    NotificationService(session).create(
                        user_id=completed.user_id,
                        notification_type="RESEARCH_COMPLETED",
                        severity="INFO",
                        title="正式研究运行已完成",
                        body="研究结果已生成，可查看报告与模拟交易方案。",
                        resource_type="RESEARCH_RUN",
                        resource_id=completed.run_id,
                        payload={"status": completed.status, "report_id": report_id},
                        dedupe_key=f"research-completed:{completed.run_id}",
                    )
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
                    if not deadline_raw:
                        from ashare_ai.orchestration.research_schedule import (
                            FreeExchangeCalendar,
                            data_readiness_wait,
                        )

                        try:
                            sessions = FreeExchangeCalendar().sessions(
                                run.trading_date, run.trading_date + timedelta(days=14)
                            )
                            wait = data_readiness_wait(
                                trading_date=run.trading_date,
                                now=now,
                                sessions=sessions,
                                retry_minutes=get_effective_settings().daily_research_retry_minutes,
                            )
                        except Exception as calendar_error:
                            run.status = "FAILED"
                            run.active_research_key = None
                            run.error_message = "authoritative trading calendar unavailable"
                            run.completed_at = now
                            AuditLogger(session).record(
                                run_id,
                                "DATA_READINESS_CALENDAR_UNAVAILABLE",
                                "Research could not calculate a safe data-readiness deadline",
                                severity="ERROR",
                                details={"error_type": type(calendar_error).__name__},
                            )
                            session.commit()
                            raise
                        deadline_raw = wait["deadline_at"]
                    deadline = datetime.fromisoformat(str(deadline_raw))
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=UTC)
                    if now < deadline:
                        retry_at = min(
                            now
                            + timedelta(
                                minutes=get_effective_settings().daily_research_retry_minutes
                            ),
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
                    run.status = "FAILED"
                    run.active_research_key = None
                    run.error_message = (
                        "benchmark data did not synchronize before the next trading session"
                    )
                    run.completed_at = now
                    AuditLogger(session).record(
                        run_id,
                        "DATA_READINESS_TIMEOUT",
                        "Benchmark data wait expired during snapshot construction",
                        severity="ERROR",
                        details={**_error_details(exc), "deadline_at": deadline.isoformat()},
                    )
                    session.commit()
                    return {"run_id": run_id, "status": "FAILED"}
        mark_research_failed(run_id, exc, session_factory=session_factory)
        raise


def run_research_job(run_id: str) -> dict[str, Any]:
    if not _retry_waiting_research(run_id):
        with SessionLocal() as session:
            run = session.get(JobRun, run_id)
            return {"run_id": run_id, "status": run.status if run is not None else "FAILED"}
    return execute_research_job(run_id, pipeline=load_pipeline())


def _load_worker_runtime() -> SystemRuntimeSettings | None:
    try:
        with SessionLocal() as session:
            return SystemConfigurationService().resolve(session)
    except Exception:
        logger.exception("could not load persisted research-worker topology; using SERIAL standby")
        return None


def _execute_isolated_research(run_id: str) -> int:
    """Keep dedicated workers isolated exactly like the serial worker."""
    return subprocess.run(
        [sys.executable, "-m", "ashare_ai.orchestration.run_job", "research", run_id],
        check=False,
    ).returncode


def consume_research_queue(*, max_standby_iterations: int | None = None) -> None:
    import redis

    runtime = _load_worker_runtime()
    settings = runtime.settings if runtime is not None else get_settings()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    if runtime is None or runtime.execution_mode != "DUAL":
        # Compose starts these replicas only through the dual-research profile.
        # If a replica is left running during an incomplete topology restart,
        # it still fails closed in SERIAL and never claims a research message.
        iterations = 0
        while max_standby_iterations is None or iterations < max_standby_iterations:
            if runtime is not None:
                publish_heartbeat(client, role="research-worker", runtime=runtime)
            iterations += 1
            if max_standby_iterations is None or iterations < max_standby_iterations:
                time.sleep(5)
        return
    queue = RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        delayed=DELAYED_QUEUE_NAME,
        lease_seconds=runtime.settings.worker_lease_seconds,
    )

    def execute(run_id: str) -> None:
        return_code = _execute_isolated_research(run_id)
        if return_code:
            logger.error("isolated research job %s exited with status %s", run_id, return_code)

    def heartbeat() -> None:
        publish_heartbeat(client, role="research-worker", runtime=runtime)

    queue.consume_forever(execute, on_poll=heartbeat)
