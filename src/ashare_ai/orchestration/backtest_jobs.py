from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.core.config import get_settings
from ashare_ai.core.user_errors import public_error_message
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.orchestration.daily import flow
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import BacktestRun, JobRun, SnapshotManifestRow

QUEUE_NAME = "ashare:backtest:pending"
PROCESSING_QUEUE_NAME = "ashare:backtest:processing"


class BacktestJobOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metrics: dict[str, Any]
    artifacts: dict[str, Any]
    output_hash: str


class BacktestJobExecutor(Protocol):
    def execute(
        self,
        *,
        backtest_id: str,
        config: dict[str, Any],
        snapshot_uris: dict[str, str],
    ) -> BacktestJobOutput: ...

    def prepare_watchlist_historical(
        self,
        *,
        backtest_id: str,
        user_id: str,
        start_date: Any,
        end_date: Any,
        config: dict[str, Any],
    ) -> dict[str, Any]: ...


def enqueue_backtest(backtest_id: str, redis_url: str | None = None) -> None:
    import redis

    client = redis.Redis.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    ).enqueue(backtest_id)


def _load_executor() -> BacktestJobExecutor:
    path = (
        os.environ.get("ASHARE_BACKTEST_EXECUTOR_FACTORY")
        or get_settings().ashare_backtest_executor_factory
    )
    if not path or ":" not in path:
        raise RuntimeError(
            "ASHARE_BACKTEST_EXECUTOR_FACTORY must be set to package.module:create_executor"
        )
    module_name, attribute = path.rsplit(":", 1)
    factory = getattr(import_module(module_name), attribute)
    return cast(BacktestJobExecutor, factory())


def mark_backtest_failed(
    backtest_id: str,
    error: Exception,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as session:
        failed = session.get(BacktestRun, backtest_id)
        if failed is None:
            return
        job = session.get(JobRun, failed.run_id) if failed.run_id else None
        if failed.status == "FAILED" and (job is None or job.status == "FAILED"):
            return
        failed.status = "FAILED"
        failed.completed_at = datetime.now(UTC)
        if job is not None:
            job.status = "FAILED"
            job.error_message = public_error_message("BACKTEST_FAILED")
            job.completed_at = datetime.now(UTC)
            AuditLogger(session).record(
                job.run_id,
                "BACKTEST_FAILED",
                "Backtest execution failed",
                severity="ERROR",
                details={
                    "backtest_id": backtest_id,
                    "error_type": type(error).__name__,
                    "retry_count": failed.retry_count,
                },
            )
        session.commit()


def execute_backtest_job(
    backtest_id: str,
    *,
    executor: BacktestJobExecutor,
    session_factory: Callable[[], Session] = SessionLocal,
) -> BacktestJobOutput:
    with session_factory() as session:
        backtest = session.get(BacktestRun, backtest_id)
        if backtest is None:
            raise KeyError(backtest_id)
        job = session.get(JobRun, backtest.run_id) if backtest.run_id else None
        config = dict(backtest.config)
        watchlist_training = config.get("training_mode") == "WATCHLIST_HISTORICAL"
        prepared_snapshot = False
        if watchlist_training and not backtest.snapshot_ids:
            prepare = getattr(executor, "prepare_watchlist_historical", None)
            if prepare is None:
                raise RuntimeError("watchlist historical training collector is unavailable")
            prepared = prepare(
                backtest_id=backtest_id,
                user_id=str(backtest.user_id or ""),
                start_date=backtest.start_date,
                end_date=backtest.end_date,
                config=config,
            )
            if not isinstance(prepared, dict):
                raise RuntimeError("watchlist historical collector returned invalid metadata")
            snapshot_ids = prepared.get("snapshot_ids")
            if not isinstance(snapshot_ids, list) or len(snapshot_ids) != 1:
                raise RuntimeError("watchlist historical collector must return one frozen snapshot")
            snapshot_id = snapshot_ids[0]
            if not isinstance(snapshot_id, str) or not snapshot_id.strip():
                raise RuntimeError("watchlist historical collector returned an invalid snapshot id")
            file_hashes = prepared.get("snapshot_file_hashes")
            if not isinstance(file_hashes, dict) or set(file_hashes) != {snapshot_id}:
                raise RuntimeError("watchlist historical collector must return one snapshot file hash")
            file_hash = file_hashes[snapshot_id]
            if not isinstance(file_hash, str) or len(file_hash) != 64:
                raise RuntimeError("watchlist historical collector returned an invalid file hash")
            snapshot_hash = prepared.get("snapshot_sha256")
            if not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64:
                raise RuntimeError("watchlist historical collector returned an invalid snapshot hash")
            backtest.snapshot_ids = [snapshot_id]
            config["snapshot_file_hashes"] = {snapshot_id: file_hash}
            config["training_snapshot_sha256"] = snapshot_hash
            backtest.config = config
            prepared_snapshot = True
            session.flush()
        manifests = list(
            session.scalars(
                select(SnapshotManifestRow).where(
                    SnapshotManifestRow.snapshot_id.in_(backtest.snapshot_ids)
                )
            )
        )
        by_id = {item.snapshot_id: item for item in manifests}
        missing = sorted(set(backtest.snapshot_ids) - set(by_id))
        uncommitted = sorted(
            snapshot_id for snapshot_id, item in by_id.items() if item.status != "COMMITTED"
        )
        if missing or uncommitted:
            raise ValueError(
                "backtest requires committed snapshots; "
                f"missing={missing}, uncommitted={uncommitted}"
            )
        if watchlist_training:
            if len(by_id) != 1 or by_id[next(iter(by_id))].dataset != "backtest_bundle":
                raise ValueError("watchlist historical training requires a backtest bundle snapshot")
            manifest = by_id[next(iter(by_id))]
            owner = manifest.details.get("user_id") if isinstance(manifest.details, dict) else None
            if owner is None or str(owner) != str(backtest.user_id or ""):
                raise ValueError("watchlist historical snapshot ownership mismatch")
            expected_hash = config.get("snapshot_file_hashes", {}).get(manifest.snapshot_id)
            actual_hash = manifest.details.get("parquet_file_sha256") if isinstance(manifest.details, dict) else None
            if not isinstance(expected_hash, str) or expected_hash != actual_hash:
                raise ValueError("watchlist historical snapshot hash metadata changed")
            expected_snapshot_hash = config.get("training_snapshot_sha256")
            if (
                not isinstance(expected_snapshot_hash, str)
                or len(expected_snapshot_hash) != 64
                or expected_snapshot_hash != manifest.payload_sha256
            ):
                raise ValueError("watchlist historical snapshot payload hash changed")
        backtest.status = "RUNNING"
        if job is not None:
            job.status = "RUNNING"
            AuditLogger(session).record(
                job.run_id,
                "BACKTEST_STARTED",
                "Backtest worker started a fixed-snapshot run",
                details={
                    "backtest_id": backtest_id,
                    "snapshot_ids": backtest.snapshot_ids,
                    "training_mode": config.get("training_mode", "SNAPSHOT"),
                    "snapshot_collected": prepared_snapshot,
                },
            )
        snapshot_uris = {
            snapshot_id: by_id[snapshot_id].parquet_uri for snapshot_id in backtest.snapshot_ids
        }
        config = {
            key: config[key]
            for key in (
                "artifact_root", "snapshot_file_hashes", "requested_start_date",
                "requested_end_date", "initial_capital", "benchmark",
            )
            if key in config
        }
        session.commit()

    try:
        output = executor.execute(
            backtest_id=backtest_id,
            config=config,
            snapshot_uris=snapshot_uris,
        )
    except Exception as exc:
        mark_backtest_failed(backtest_id, exc, session_factory=session_factory)
        raise

    with session_factory() as session:
        completed = session.get(BacktestRun, backtest_id)
        if completed is None:
            raise KeyError(backtest_id)
        completed.status = "SUCCEEDED"
        completed.metrics = output.metrics
        completed.artifacts = output.artifacts
        completed.output_hash = output.output_hash
        completed.completed_at = datetime.now(UTC)
        if completed.run_id:
            job = session.get(JobRun, completed.run_id)
            if job is not None:
                job.status = "SUCCEEDED"
                job.output_hash = output.output_hash
                job.completed_at = datetime.now(UTC)
                AuditLogger(session).record(
                    job.run_id,
                    "BACKTEST_COMPLETED",
                    "Backtest completed successfully",
                    details={"backtest_id": backtest_id, "output_hash": output.output_hash},
                )
        session.commit()
    return output


@flow(name="ashare-fixed-snapshot-backtest", log_prints=True)
def run_backtest_job(backtest_id: str) -> dict[str, Any]:
    try:
        executor = _load_executor()
        return execute_backtest_job(backtest_id, executor=executor).model_dump(mode="json")
    except Exception as exc:
        mark_backtest_failed(backtest_id, exc)
        raise


def consume_backtest_queue() -> None:
    import redis

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    queue = RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )
    queue.consume_forever(run_backtest_job)
