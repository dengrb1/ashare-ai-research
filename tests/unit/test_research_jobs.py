from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.orchestration.research_jobs import execute_research_job
from ashare_ai.storage.models import AuditEvent, Base, JobRun


class Pipeline:
    def __init__(self, fail_at: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    def _value(self, name: str, value):
        self.calls.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"failed at {name}")
        return value

    def sync_reference_data(self, run_id):
        return self._value("sync", None)

    def ingest_and_verify(self, run_id):
        return self._value("ingest", ["snapshot"])

    def build_universe(self, run_id, snapshots):
        return self._value("universe", "universe")

    def build_features(self, run_id, universe_id):
        return self._value("features", "features")

    def run_research_agents(self, run_id, feature_snapshot_id):
        return self._value("agents", "agents")

    def calculate_scores(self, run_id, agent_bundle_id):
        return self._value("scores", "scores")

    def qlib_filter(self, run_id, score_snapshot_id):
        return self._value("qlib", "candidates")

    def risk_state(self, run_id):
        return self._value("risk", "NORMAL")

    def build_portfolio(self, run_id, candidate_snapshot_id):
        return self._value("portfolio", "portfolio")

    def publish_report(self, run_id, portfolio_id, risk_state):
        return self._value("report", "report")

    def complete_run(self, run_id, report_id, status):
        self.calls.append("complete")
        return {"run_id": run_id, "status": status}


def _factory():
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            JobRun(
                run_id="research-run",
                run_type="DAILY",
                trading_date=date(2026, 7, 14),
                decision_at=now,
                status="PENDING",
                idempotency_key="research-key",
                manifest={"frozen": True},
                input_hash="a" * 64,
                started_at=now,
            )
        )
        session.commit()
    return factory


def test_research_worker_executes_existing_frozen_run() -> None:
    factory = _factory()
    pipeline = Pipeline()
    result = execute_research_job("research-run", pipeline=pipeline, session_factory=factory)
    assert result["status"] == "SUCCEEDED"
    assert pipeline.calls == [
        "sync",
        "ingest",
        "universe",
        "features",
        "agents",
        "scores",
        "qlib",
        "risk",
        "portfolio",
        "report",
        "complete",
    ]
    with factory() as session:
        events = session.query(AuditEvent).all()
        assert events[0].event_type == "RESEARCH_STARTED"


def test_research_worker_persists_failure_reason() -> None:
    factory = _factory()
    with pytest.raises(RuntimeError, match="failed at features"):
        execute_research_job(
            "research-run",
            pipeline=Pipeline(fail_at="features"),
            session_factory=factory,
        )
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None
        assert run.status == "FAILED"
        assert run.error_message == "failed at features"
        assert (
            session.query(AuditEvent).order_by(AuditEvent.created_at.desc()).first().severity
            == "ERROR"
        )
