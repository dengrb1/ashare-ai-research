from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.config import Settings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration import production
from ashare_ai.orchestration.akshare_bundle import MarketDataAcquisitionError
from ashare_ai.orchestration.production import ApplicationPipeline
from ashare_ai.storage.models import AuditEvent, Base, JobRun


class Backend:
    def run_manifest(self, trading_date, decision_at):
        return {"trading_date": trading_date.isoformat(), "decision_at": decision_at.isoformat()}

    def sync_reference_data(self, run_id):
        return None

    def ingest_and_verify(self, run_id):
        return ["snapshot"]

    def build_universe(self, run_id, snapshot_ids):
        return "universe"

    def build_features(self, run_id, universe_id):
        return "features"

    def run_research_agents(self, run_id, feature_snapshot_id):
        return "agents"

    def calculate_scores(self, run_id, agent_bundle_id):
        return "scores"

    def qlib_filter(self, run_id, score_snapshot_id):
        return "candidates"

    def risk_state(self, run_id):
        return "NORMAL"

    def build_portfolio(self, run_id, candidate_snapshot_id):
        return "portfolio"

    def publish_report(self, run_id, portfolio_id, risk_state):
        return "report"


def test_application_pipeline_persists_stage_and_run_audit(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    monkeypatch.setattr(
        "ashare_ai.orchestration.production._execution_id", lambda: "prefect-flow-run-1"
    )
    pipeline = ApplicationPipeline(Backend(), factory)
    run_id = pipeline.start_run(date(2026, 7, 14))
    assert pipeline.start_run(date(2026, 7, 14)) == run_id
    pipeline.sync_reference_data(run_id)
    snapshots = pipeline.ingest_and_verify(run_id)
    result = pipeline.complete_run(run_id, snapshots[0], "SUCCEEDED")
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None and run.status == "SUCCEEDED"
        event_types = [event.event_type for event in session.query(AuditEvent).all()]
        assert session.query(JobRun).count() == 1
    assert result["status"] == "SUCCEEDED"
    assert event_types == ["RUN_STARTED", "STAGE_COMPLETED", "STAGE_COMPLETED", "RUN_COMPLETED"]


def test_same_day_akshare_run_starts_after_configured_close_time(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    class BeforeClose(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 15, 15, 4, tzinfo=SHANGHAI)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(production, "datetime", BeforeClose)
    monkeypatch.setattr(
        production,
        "get_settings",
        lambda: Settings(canonical_bundle_mode="akshare"),
    )
    pipeline = ApplicationPipeline(Backend(), factory)
    with pytest.raises(RuntimeError, match="15:05"):
        pipeline.start_run(date(2026, 7, 15))
    with pytest.raises(RuntimeError, match="cannot be in the future"):
        pipeline.start_run(date(2026, 7, 16))

    class AfterClose(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz == UTC:
                return datetime(2026, 7, 15, 8, 30, tzinfo=UTC)
            value = datetime(2026, 7, 15, 16, 30, tzinfo=SHANGHAI)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(production, "datetime", AfterClose)
    monkeypatch.setattr(production, "_execution_id", lambda: "same-day-after-close")
    run_id = pipeline.start_run(date(2026, 7, 15))
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None
        assert run.decision_at.hour == 16
        assert run.decision_at.minute == 30


def test_acquisition_failure_is_sanitized_and_audited(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    class FailingBackend(Backend):
        def sync_reference_data(self, run_id):
            del run_id
            raise MarketDataAcquisitionError(
                operation="benchmark_bars",
                subject="000300",
                attempt_count=4,
                sources=("eastmoney", "sina"),
            )

    monkeypatch.setattr(production, "_execution_id", lambda: "sanitized-acquisition")
    pipeline = ApplicationPipeline(FailingBackend(), factory)
    run_id = pipeline.start_run(date(2026, 7, 14))
    with pytest.raises(MarketDataAcquisitionError):
        pipeline.sync_reference_data(run_id)

    with factory() as session:
        run = session.get(JobRun, run_id)
        event = session.query(AuditEvent).filter_by(event_type="STAGE_FAILED").one()
        assert run is not None and run.status == "FAILED"
        assert "000300" in str(run.error_message)
        assert "http" not in str(run.error_message).lower()
        assert event.details == {
            "stage": "sync_reference_data",
            "error_type": "MarketDataAcquisitionError",
            "operation": "benchmark_bars",
            "subject": "000300",
            "attempt_count": 4,
            "sources": ["eastmoney", "sina"],
        }
