from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.user_errors import public_error_message
from ashare_ai.orchestration.research_jobs import _retry_waiting_research, execute_research_job
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


def _waiting_factory(*, deadline: datetime):
    factory = _factory()
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None
        run.status = "DATA_READINESS_WAITING"
        run.active_research_key = "active-research-key"
        run.manifest = {
            "frozen": True,
            "data_readiness_wait": {
                "deadline_at": deadline.isoformat(),
                "next_retry_at": None,
                "attempt_count": 0,
            },
        }
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


def test_research_worker_skips_portfolio_for_small_targeted_research() -> None:
    class ResearchOnlyPipeline(Pipeline):
        def portfolio_requested(self, run_id):
            del run_id
            return False

    factory = _factory()
    pipeline = ResearchOnlyPipeline()
    result = execute_research_job("research-run", pipeline=pipeline, session_factory=factory)
    assert result["status"] == "SUCCEEDED"
    assert "portfolio" not in pipeline.calls
    assert pipeline.calls[-2:] == ["report", "complete"]


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
        # The user-facing failure reason is fixed Chinese copy; the exception text
        # stays in the structured audit details for operations.
        assert run.error_message == public_error_message("RESEARCH_FAILED")
        assert (
            session.query(AuditEvent).order_by(AuditEvent.created_at.desc()).first().severity
            == "ERROR"
        )


def test_data_readiness_wait_continues_after_the_legacy_two_hour_window() -> None:
    deadline = datetime(2026, 7, 20, 1, 25, tzinfo=UTC)
    factory = _waiting_factory(deadline=deadline)
    queued: list[tuple[str, datetime]] = []
    now = datetime(2026, 7, 17, 9, 10, tzinfo=UTC)

    ready = _retry_waiting_research(
        "research-run",
        now=now,
        session_factory=factory,
        readiness=lambda _date, _checked_at: False,
        enqueue_at=lambda run_id, retry_at: queued.append((run_id, retry_at)),
    )

    assert ready is False
    assert queued and queued[0][0] == "research-run"
    assert queued[0][1] == now + timedelta(minutes=5)
    assert now < queued[0][1] < deadline
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None and run.status == "DATA_READINESS_WAITING"
        assert run.manifest["data_readiness_wait"]["attempt_count"] == 1


def test_data_readiness_retry_uses_batch_friendly_exponential_backoff() -> None:
    deadline = datetime(2026, 7, 20, 1, 25, tzinfo=UTC)
    factory = _waiting_factory(deadline=deadline)
    queued: list[tuple[str, datetime]] = []
    first = datetime(2026, 7, 17, 9, 10, tzinfo=UTC)

    for current in (first, first + timedelta(minutes=5), first + timedelta(minutes=25)):
        _retry_waiting_research(
            "research-run",
            now=current,
            session_factory=factory,
            readiness=lambda _date, _checked_at: False,
            enqueue_at=lambda run_id, retry_at: queued.append((run_id, retry_at)),
        )

    assert [retry_at - current for (_, retry_at), current in zip(queued, (
        first,
        first + timedelta(minutes=5),
        first + timedelta(minutes=25),
    ), strict=True)] == [
        timedelta(minutes=5),
        timedelta(minutes=20),
        timedelta(minutes=80),
    ]


def test_data_readiness_wait_promotes_late_data_before_next_session_cutoff() -> None:
    deadline = datetime(2026, 7, 20, 1, 25, tzinfo=UTC)
    factory = _waiting_factory(deadline=deadline)

    ready = _retry_waiting_research(
        "research-run",
        now=deadline - timedelta(minutes=1),
        session_factory=factory,
        readiness=lambda _date, _checked_at: True,
    )

    assert ready is True
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None and run.status == "PENDING"
        assert run.manifest["data_readiness_wait"]["next_retry_at"] is None


def test_data_readiness_wait_fails_at_next_session_cutoff() -> None:
    deadline = datetime(2026, 7, 20, 1, 25, tzinfo=UTC)
    factory = _waiting_factory(deadline=deadline)

    ready = _retry_waiting_research(
        "research-run",
        now=deadline,
        session_factory=factory,
        readiness=lambda _date, _checked_at: False,
    )

    assert ready is False
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None
        assert run.status == "FAILED"
        assert run.error_message == public_error_message("DATA_READINESS_TIMEOUT")
        assert run.audit_events[-1].event_type == "DATA_READINESS_TIMEOUT"


def test_data_readiness_wait_accepts_data_that_arrives_at_cutoff() -> None:
    deadline = datetime(2026, 7, 20, 1, 25, tzinfo=UTC)
    factory = _waiting_factory(deadline=deadline)

    ready = _retry_waiting_research(
        "research-run",
        now=deadline,
        session_factory=factory,
        readiness=lambda _date, _checked_at: True,
    )

    assert ready is True
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None and run.status == "PENDING"
