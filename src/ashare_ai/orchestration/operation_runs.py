from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from ashare_ai.core.hashing import stable_hash
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.storage.models import JobRun


def create_operation_run(
    session: Session,
    *,
    user_id: str,
    run_type: str,
    resource_id: str,
    trading_date: date,
    decision_at: datetime,
    input_hash: str,
    manifest: dict[str, Any],
    created_at: datetime | None = None,
) -> JobRun:
    now = created_at or datetime.now(UTC)
    run = JobRun(
        run_id=str(uuid4()),
        user_id=user_id,
        run_type=run_type,
        trading_date=trading_date,
        decision_at=decision_at,
        status="PENDING",
        idempotency_key=stable_hash(
            {"kind": "OPERATION_RUN", "run_type": run_type, "resource_id": resource_id}
        ),
        manifest={**manifest, "resource_id": resource_id},
        input_hash=input_hash,
        started_at=now,
    )
    session.add(run)
    session.flush()
    AuditLogger(session).record(
        run.run_id,
        f"{run_type}_SUBMITTED",
        f"{run_type.replace('_', ' ').title()} submitted",
        details={"resource_id": resource_id, "input_hash": input_hash},
    )
    return run


def transition_operation_run(
    session: Session,
    operation_run_id: str | None,
    *,
    status: str,
    event_type: str,
    message: str,
    details: dict[str, Any] | None = None,
    output_hash: str | None = None,
    error_message: str | None = None,
) -> None:
    if not operation_run_id:
        return
    run = session.get(JobRun, operation_run_id)
    if run is None:
        return
    run.status = status
    run.output_hash = output_hash or run.output_hash
    run.error_message = error_message
    if status in {"SUCCEEDED", "FAILED", "UNAVAILABLE", "CANCELLED"}:
        run.completed_at = datetime.now(UTC)
    AuditLogger(session).record(
        run.run_id,
        event_type,
        message,
        severity="ERROR" if status in {"FAILED", "UNAVAILABLE"} else "INFO",
        details=details or {},
    )
