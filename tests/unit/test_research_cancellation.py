from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import AuthContext
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.orchestration.research_jobs import execute_research_job
from ashare_ai.storage.models import AuditEvent, Base, JobRun


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _add_run(
    factory: sessionmaker[Session],
    *,
    status: str,
    user_id: str = "owner",
) -> None:
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            JobRun(
                run_id="research-run",
                user_id=user_id,
                active_research_key="active-key",
                run_type="DAILY",
                trading_date=date(2026, 7, 17),
                decision_at=now,
                status=status,
                idempotency_key="research-cancel-key",
                manifest={},
                input_hash="a" * 64,
                started_at=now,
            )
        )
        session.commit()


def _context(user_id: str) -> AuthContext:
    return AuthContext(
        user=cast(
            Any,
            SimpleNamespace(
                user_id=user_id,
                username=user_id,
                role="ADMIN",
                enabled=True,
            ),
        ),
        session=cast(Any, SimpleNamespace()),
    )


def _override(factory: sessionmaker[Session], user_id: str) -> None:
    def override_db():
        with factory() as session:
            yield session

    context = _context(user_id)
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_auth_context] = lambda: context
    app.dependency_overrides[get_write_context] = lambda: context


def test_queued_research_can_be_cancelled_and_is_audited() -> None:
    factory = _factory()
    _add_run(factory, status="PENDING")
    _override(factory, "owner")
    try:
        response = TestClient(app).post("/api/v1/research/runs/research-run/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        assert response.json()["phase"] == "研究已停止"
        with factory() as session:
            run = session.get(JobRun, "research-run")
            assert run is not None
            assert run.active_research_key is None
            assert run.completed_at is not None
            assert [event.event_type for event in session.query(AuditEvent).all()] == [
                "RESEARCH_CANCEL_REQUESTED",
                "RESEARCH_CANCELLED",
            ]
    finally:
        app.dependency_overrides.clear()


def test_running_cancel_is_owner_only_and_terminal_transitions_conflict() -> None:
    factory = _factory()
    _add_run(factory, status="RUNNING")
    _override(factory, "other-user")
    try:
        assert (
            TestClient(app).post("/api/v1/research/runs/research-run/cancel").status_code
            == 404
        )
    finally:
        app.dependency_overrides.clear()

    _override(factory, "owner")
    try:
        client = TestClient(app)
        requested = client.post("/api/v1/research/runs/research-run/cancel")
        assert requested.status_code == 200
        assert requested.json()["status"] == "CANCEL_REQUESTED"
        assert "当前阶段完成后" in requested.json()["phase"]
        assert client.post("/api/v1/research/runs/research-run/cancel").status_code == 409
    finally:
        app.dependency_overrides.clear()

    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None
        run.status = "SUCCEEDED"
        session.commit()
    _override(factory, "owner")
    try:
        assert (
            TestClient(app).post("/api/v1/research/runs/research-run/cancel").status_code
            == 409
        )
    finally:
        app.dependency_overrides.clear()


class _Pipeline:
    def __init__(self, factory: sessionmaker[Session], *, fail_after_request: bool = False) -> None:
        self.factory = factory
        self.fail_after_request = fail_after_request
        self.calls: list[str] = []

    def sync_reference_data(self, run_id: str) -> None:
        self.calls.append("sync")
        with self.factory() as session:
            run = session.get(JobRun, run_id)
            assert run is not None
            run.status = "CANCEL_REQUESTED"
            session.commit()
        if self.fail_after_request:
            raise RuntimeError("stage stopped with upstream error")

    def ingest_and_verify(self, run_id: str) -> list[str]:
        self.calls.append("ingest")
        return []

    def build_universe(self, run_id: str, snapshots: list[str]) -> str:
        raise AssertionError("must stop before universe")

    def build_features(self, run_id: str, universe_id: str) -> str:
        raise AssertionError("must stop before features")

    def run_research_agents(self, run_id: str, feature_snapshot_id: str) -> str:
        raise AssertionError("must stop before agents")

    def calculate_scores(self, run_id: str, agent_bundle_id: str) -> str:
        raise AssertionError("must stop before scores")

    def qlib_filter(self, run_id: str, score_snapshot_id: str) -> str:
        raise AssertionError("must stop before candidates")

    def risk_state(self, run_id: str) -> str:
        raise AssertionError("must stop before risk")

    def build_portfolio(self, run_id: str, candidate_snapshot_id: str) -> str:
        raise AssertionError("must stop before portfolio")

    def publish_report(self, run_id: str, portfolio_id: str | None, risk_state: str) -> str:
        raise AssertionError("must stop before report")

    def complete_run(self, run_id: str, report_id: str, status: str) -> dict[str, str]:
        raise AssertionError("must stop before completion")


def test_worker_stops_at_stage_boundary_and_clears_active_key() -> None:
    factory = _factory()
    _add_run(factory, status="PENDING")
    pipeline = _Pipeline(factory)
    result = execute_research_job("research-run", pipeline=pipeline, session_factory=factory)
    assert result == {"run_id": "research-run", "status": "CANCELLED"}
    assert pipeline.calls == ["sync"]
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None
        assert run.status == "CANCELLED"
        assert run.active_research_key is None
        assert run.completed_at is not None
        cancelled = session.query(AuditEvent).filter_by(event_type="RESEARCH_CANCELLED").one()
        assert cancelled.details["boundary"] == "sync_reference_data"


@pytest.mark.parametrize("initial_status", ["CANCEL_REQUESTED", "CANCELLED"])
def test_worker_claim_does_not_revive_cancelled_research(initial_status: str) -> None:
    factory = _factory()
    _add_run(factory, status=initial_status)
    pipeline = _Pipeline(factory)
    result = execute_research_job("research-run", pipeline=pipeline, session_factory=factory)
    assert result == {"run_id": "research-run", "status": "CANCELLED"}
    assert pipeline.calls == []
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None
        assert run.status == "CANCELLED"
        if initial_status == "CANCEL_REQUESTED":
            assert run.active_research_key is None
            assert run.completed_at is not None


def test_stage_error_does_not_overwrite_cancel_request_with_failed() -> None:
    factory = _factory()
    _add_run(factory, status="PENDING")
    with pytest.raises(RuntimeError, match="upstream error"):
        execute_research_job(
            "research-run",
            pipeline=_Pipeline(factory, fail_after_request=True),
            session_factory=factory,
        )
    with factory() as session:
        run = session.get(JobRun, "research-run")
        assert run is not None
        assert run.status == "CANCELLED"
        assert run.error_message is None
