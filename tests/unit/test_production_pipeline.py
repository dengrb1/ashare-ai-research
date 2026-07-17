from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.config import Settings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration import production
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


def test_same_day_akshare_run_never_uses_a_future_decision_time(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 15, 16, 30, tzinfo=SHANGHAI)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(production, "datetime", Morning)
    monkeypatch.setattr(
        production,
        "get_settings",
        lambda: Settings(canonical_bundle_mode="akshare"),
    )
    pipeline = ApplicationPipeline(Backend(), factory)
    with pytest.raises(RuntimeError, match="after 17:00"):
        pipeline.start_run(date(2026, 7, 15))
    with pytest.raises(RuntimeError, match="cannot be in the future"):
        pipeline.start_run(date(2026, 7, 16))

    class Evening(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz == UTC:
                return datetime(2026, 7, 15, 9, 15, tzinfo=UTC)
            value = datetime(2026, 7, 15, 17, 15, tzinfo=SHANGHAI)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(production, "datetime", Evening)
    monkeypatch.setattr(production, "_execution_id", lambda: "same-day-evening")
    run_id = pipeline.start_run(date(2026, 7, 15))
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None
        assert run.decision_at.hour == 17
        assert run.decision_at.minute == 15
