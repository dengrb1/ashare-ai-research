from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.user_errors import public_error_message
from ashare_ai.orchestration.backtest_jobs import (
    BacktestJobOutput,
    execute_backtest_job,
    mark_backtest_failed,
)
from ashare_ai.storage.models import AuditEvent, BacktestRun, Base, JobRun, SnapshotManifestRow


class Executor:
    def execute(self, *, backtest_id, config, snapshot_uris):
        assert snapshot_uris == {"snapshot-1": "file:///snapshot.parquet"}
        return BacktestJobOutput(
            metrics={"sharpe": 1.2},
            artifacts={"nav": "s3://bucket/nav.parquet"},
            output_hash="f" * 64,
        )


class WatchlistExecutor:
    def __init__(self) -> None:
        self.prepare_calls = 0

    def prepare_watchlist_historical(self, *, backtest_id, user_id, start_date, end_date, config):
        assert (backtest_id, user_id) == ("watchlist-backtest", "user-1")
        assert start_date == date(2025, 1, 1)
        assert end_date == date(2025, 12, 31)
        assert config["training_mode"] == "WATCHLIST_HISTORICAL"
        self.prepare_calls += 1
        return {
            "snapshot_ids": ["watchlist-snapshot"],
            "snapshot_file_hashes": {"watchlist-snapshot": "d" * 64},
            "snapshot_sha256": "e" * 64,
        }

    def execute(self, *, backtest_id, config, snapshot_uris):
        assert backtest_id == "watchlist-backtest"
        assert snapshot_uris == {"watchlist-snapshot": "file:///watchlist.parquet"}
        assert config["snapshot_file_hashes"] == {"watchlist-snapshot": "d" * 64}
        return BacktestJobOutput(metrics={"sessions": 240}, artifacts={}, output_hash="f" * 64)


def test_backtest_worker_consumes_committed_snapshot_and_updates_audit() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            SnapshotManifestRow(
                snapshot_id="snapshot-1",
                dataset="fixture",
                source="fixture",
                schema_version="1",
                adapter_version="1",
                fetched_at=now,
                row_count=1,
                payload_sha256="a" * 64,
                parquet_uri="file:///snapshot.parquet",
                status="COMMITTED",
                details={},
            )
        )
        session.add(
            JobRun(
                run_id="backtest-run",
                run_type="BACKTEST",
                trading_date=date(2025, 12, 31),
                decision_at=now,
                status="PENDING",
                idempotency_key="backtest-key",
                manifest={},
                input_hash="b" * 64,
                started_at=now,
            )
        )
        session.add(
            BacktestRun(
                backtest_id="backtest-1",
                run_id="backtest-run",
                name="fixed",
                status="PENDING",
                start_date=date(2025, 1, 1),
                end_date=date(2025, 12, 31),
                config={},
                snapshot_ids=["snapshot-1"],
                input_hash="b" * 64,
                created_at=now,
            )
        )
        session.commit()
    output = execute_backtest_job("backtest-1", executor=Executor(), session_factory=factory)
    with factory() as session:
        backtest = session.get(BacktestRun, "backtest-1")
        run = session.get(JobRun, "backtest-run")
        assert backtest is not None and backtest.status == "SUCCEEDED"
        assert run is not None and run.status == "SUCCEEDED"
    assert output.metrics["sharpe"] == 1.2


def test_watchlist_worker_collects_one_owned_frozen_snapshot_before_execution() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            SnapshotManifestRow(
                snapshot_id="watchlist-snapshot",
                dataset="backtest_bundle",
                source="collector",
                schema_version="1",
                adapter_version="1",
                fetched_at=now,
                row_count=1,
                payload_sha256="e" * 64,
                parquet_uri="file:///watchlist.parquet",
                status="COMMITTED",
                details={
                    "user_id": "user-1",
                    "parquet_file_sha256": "d" * 64,
                },
            )
        )
        session.add(
            JobRun(
                run_id="watchlist-run",
                run_type="BACKTEST",
                trading_date=date(2025, 12, 31),
                decision_at=now,
                status="PENDING",
                idempotency_key="watchlist-key",
                manifest={},
                input_hash="b" * 64,
                started_at=now,
            )
        )
        session.add(
            BacktestRun(
                backtest_id="watchlist-backtest",
                user_id="user-1",
                run_id="watchlist-run",
                name="watchlist",
                status="PENDING",
                start_date=date(2025, 1, 1),
                end_date=date(2025, 12, 31),
                config={"training_mode": "WATCHLIST_HISTORICAL", "symbols": ["600000.SH"]},
                snapshot_ids=[],
                input_hash="b" * 64,
                created_at=now,
            )
        )
        session.commit()
    executor = WatchlistExecutor()
    output = execute_backtest_job(
        "watchlist-backtest", executor=executor, session_factory=factory
    )
    assert output.metrics["sessions"] == 240
    assert executor.prepare_calls == 1
    with factory() as session:
        row = session.get(BacktestRun, "watchlist-backtest")
        assert row is not None
        assert row.snapshot_ids == ["watchlist-snapshot"]
        assert row.config["training_snapshot_sha256"] == "e" * 64


def test_executor_load_or_snapshot_failure_is_persisted() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            JobRun(
                run_id="failed-run",
                run_type="BACKTEST",
                trading_date=date(2025, 12, 31),
                decision_at=now,
                status="PENDING",
                idempotency_key="failed-key",
                manifest={},
                input_hash="c" * 64,
                started_at=now,
            )
        )
        session.add(
            BacktestRun(
                backtest_id="failed-backtest",
                run_id="failed-run",
                name="failed",
                status="PENDING",
                start_date=date(2025, 1, 1),
                end_date=date(2025, 12, 31),
                config={},
                snapshot_ids=["missing"],
                input_hash="c" * 64,
                created_at=now,
            )
        )
        session.commit()
    mark_backtest_failed(
        "failed-backtest", RuntimeError("executor unavailable"), session_factory=factory
    )
    with factory() as session:
        backtest = session.get(BacktestRun, "failed-backtest")
        run = session.get(JobRun, "failed-run")
        assert backtest is not None and backtest.status == "FAILED"
        assert run is not None and run.status == "FAILED"
        # User-facing backtest failure is fixed Chinese copy; the executor
        # exception stays in the audit details.
        assert run.error_message == public_error_message("BACKTEST_FAILED")
    mark_backtest_failed(
        "failed-backtest", RuntimeError("executor unavailable"), session_factory=factory
    )
    with factory() as session:
        events = session.query(AuditEvent).filter(AuditEvent.event_type == "BACKTEST_FAILED").all()
        assert len(events) == 1
