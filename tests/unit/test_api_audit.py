from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import AuthContext
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.core.contracts import AgentComponentResult, EvidenceRef
from ashare_ai.core.hashing import sha256_bytes
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.storage.models import (
    AuditEvent,
    BacktestRun,
    Base,
    CandidateRow,
    EvidenceRow,
    JobRun,
    PortfolioRow,
    ReportRow,
    ScoreRow,
    SnapshotManifestRow,
)
from ashare_ai.storage.object_service import StoredObjectService
from ashare_ai.storage.objects import LocalObjectStore

SHA = "b" * 64


def _override_authenticated_admin(session: Session) -> None:
    def override_db():
        yield session

    context = AuthContext(
        user=cast(
            Any,
            SimpleNamespace(
                user_id="legacy-test-admin",
                username="legacy-test-admin",
                role="ADMIN",
                enabled=True,
            ),
        ),
        session=cast(Any, SimpleNamespace()),
    )
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_auth_context] = lambda: context
    app.dependency_overrides[get_write_context] = lambda: context


def _add_backtest_snapshot(session: Session) -> None:
    now = datetime.now(UTC)
    session.add(
        JobRun(
            run_id="snapshot-owner-run",
            run_type="DAILY",
            trading_date=date(2025, 12, 31),
            decision_at=now,
            status="SUCCEEDED",
            idempotency_key="snapshot-owner-key",
            manifest={},
            input_hash="9" * 64,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        SnapshotManifestRow(
            snapshot_id="snapshot-1",
            run_id="snapshot-owner-run",
            dataset="backtest_bundle",
            source="fixture",
            schema_version="1",
            adapter_version="1",
            fetched_at=now,
            row_count=1,
            payload_sha256="8" * 64,
            parquet_uri="file:///fixture.parquet",
            status="COMMITTED",
            details={
                "parquet_file_sha256": "7" * 64,
                "calendar_start": "2025-01-01",
                "calendar_end": "2025-12-31",
                "executable_signal_count": 1,
            },
            committed_at=now,
        )
    )
    session.commit()


def test_api_exposes_score_lineage_and_run_audit() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    run = JobRun(
        run_id="run-1",
        run_type="DAILY",
        trading_date=date(2026, 7, 14),
        decision_at=now,
        status="SUCCEEDED",
        idempotency_key="key-1",
        manifest={},
        input_hash=SHA,
        output_hash="c" * 64,
        started_at=now,
        completed_at=now,
    )
    score = ScoreRow(
        run_id="run-1",
        symbol="600000.SH",
        trading_date=date(2026, 7, 14),
        decision_at=now,
        fundamental_score=70,
        technical_score=80,
        sentiment_score=60,
        quality_confidence_score=90,
        total_score=73,
        formula_version="v1",
        agent_bundle_sha256="d" * 64,
        evidence_bundle_sha256="e" * 64,
        feature_snapshot_id="snapshot-1",
    )
    evidence = EvidenceRow(
        evidence_id="evidence-1",
        run_id="run-1",
        symbol="600000.SH",
        component="fundamental",
        evidence_type="financial_fact",
        source="exchange",
        source_record_id="report-1",
        available_at=now,
        payload_sha256="e" * 64,
        excerpt="audited revenue",
        object_uri="s3://bucket/object",
    )
    session.add_all([run, score, evidence])
    session.flush()
    AuditLogger(session).record("run-1", "RUN_COMPLETED", "Daily run completed")
    session.commit()

    _override_authenticated_admin(session)
    try:
        client = TestClient(app)
        score_response = client.get("/api/v1/scores/2026-07-14/600000.SH")
        assert score_response.status_code == 200
        assert score_response.json()["total_score"] == 73
        lineage = client.get("/api/v1/scores/2026-07-14/600000.SH/lineage")
        assert lineage.json()["evidence_bundle_sha256"] == "e" * 64
        assert lineage.json()["evidence"][0]["source_record_id"] == "report-1"
        audit = client.get("/api/v1/runs/run-1/audit")
        assert audit.status_code == 200
        assert audit.json()[0]["event_type"] == "RUN_COMPLETED"
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_backtest_submission_is_idempotent_and_audited(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    _add_backtest_snapshot(session)

    _override_authenticated_admin(session)
    queued: list[str] = []
    monkeypatch.setattr("ashare_ai.api.app.enqueue_backtest", queued.append)
    payload = {
        "name": "fixed snapshot",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "snapshot_ids": ["snapshot-1"],
        "config": {"seed": 42},
    }
    try:
        client = TestClient(app)
        headers = {"Idempotency-Key": "mobile-backtest-1"}
        first = client.post("/api/v1/backtests", json=payload, headers=headers)
        second = client.post("/api/v1/backtests", json=payload, headers=headers)
        assert first.status_code == 202
        assert second.status_code == 200
        assert first.json()["backtest_id"] == second.json()["backtest_id"]
        assert first.json()["run_id"]
        assert first.json()["completed_at"] is None
        assert first.json()["error_message"] is None
        assert queued == [first.json()["backtest_id"]]
        conflicting = client.post(
            "/api/v1/backtests", json={**payload, "name": "different"}, headers=headers
        )
        assert conflicting.status_code == 409
        run_id = session.query(JobRun).filter(JobRun.run_type == "BACKTEST").one().run_id
        audit = client.get(f"/api/v1/runs/{run_id}/audit")
        assert audit.json()[0]["event_type"] == "BACKTEST_SUBMITTED"
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_agent_cost_and_evidence_are_persisted(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    now = datetime(2026, 7, 14, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
    session.add(
        JobRun(
            run_id="agent-run",
            run_type="DAILY",
            trading_date=date(2026, 7, 14),
            decision_at=now,
            status="RUNNING",
            idempotency_key="agent-key",
            manifest={},
            input_hash="a" * 64,
            started_at=now,
        )
    )
    payload = b"audited financial fact"
    object_row = StoredObjectService(session, LocalObjectStore(tmp_path / "objects")).put(
        payload,
        content_type="application/json",
        source="exchange",
        source_record_id="fact-1",
        fetched_at=now,
        available_at=now,
    )
    result = AgentComponentResult(
        component="fundamental",
        score=80,
        confidence=0.9,
        evidence=(
            EvidenceRef(
                evidence_id="agent-evidence",
                evidence_type="financial_fact",
                source="exchange",
                source_record_id="fact-1",
                available_at=now,
                payload_sha256=sha256_bytes(payload),
            ),
        ),
        model_provider="openai-compatible",
        model_name="model",
        reasoning_effort="high",
        prompt_version="v1",
        prompt_sha256="c" * 64,
        response_sha256="d" * 64,
        input_tokens=100,
        output_tokens=20,
        duration_ms=300,
        retry_count=1,
    )
    call = AuditLogger(session).record_agent_result(
        run_id="agent-run",
        symbol="600000.SH",
        request_sha256="e" * 64,
        result=result,
        created_at=now,
    )
    session.commit()
    assert call.reasoning_effort == "high"
    assert call.input_tokens == 100
    evidence = session.query(EvidenceRow).one()
    assert evidence.source_record_id == "fact-1"
    assert evidence.object_occurrence_id is not None
    assert evidence.object_uri == object_row.object_uri


def test_backtest_enqueue_failure_does_not_leave_pending(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    _add_backtest_snapshot(session)

    def fail_enqueue(backtest_id: str) -> None:
        del backtest_id
        raise RuntimeError("redis unavailable")

    _override_authenticated_admin(session)
    monkeypatch.setattr("ashare_ai.api.app.enqueue_backtest", fail_enqueue)
    try:
        response = TestClient(app).post(
            "/api/v1/backtests",
            json={
                "name": "queue failure",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "snapshot_ids": ["snapshot-1"],
                "config": {},
            },
        )
        assert response.status_code == 503
        failed_run = session.query(JobRun).filter(JobRun.run_type == "BACKTEST").one()
        assert failed_run.status == "FAILED"
        assert session.query(BacktestRun).one().status == "FAILED"
        events = TestClient(app).get(f"/api/v1/runs/{failed_run.run_id}/audit").json()
        assert events[-1]["event_type"] == "BACKTEST_ENQUEUE_FAILED"
        assert events[-1]["severity"] == "ERROR"
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_daily_result_endpoints_select_latest_successful_run() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    trading_date = date(2026, 7, 14)
    now = datetime(2026, 7, 14, 10, tzinfo=UTC)
    _add_result_set(session, "old", trading_date, now, "SUCCEEDED", 61)
    _add_result_set(session, "new", trading_date, now + timedelta(minutes=1), "SUCCEEDED", 82)
    _add_result_set(session, "failed", trading_date, now + timedelta(minutes=2), "FAILED", 99)
    session.add(
        JobRun(
            run_id="backtest-later",
            run_type="BACKTEST",
            trading_date=trading_date,
            decision_at=now + timedelta(minutes=3),
            status="SUCCEEDED",
            idempotency_key="key-backtest-later",
            manifest={},
            input_hash="f" * 64,
            output_hash="f" * 64,
            started_at=now + timedelta(minutes=2),
            completed_at=now + timedelta(minutes=3),
        )
    )
    session.commit()

    _override_authenticated_admin(session)
    try:
        client = TestClient(app)
        latest = client.get(f"/api/v1/scores/{trading_date}/600000.SH")
        assert latest.status_code == 200
        assert latest.json()["total_score"] == 82
        explicit = client.get(f"/api/v1/scores/{trading_date}/600000.SH", params={"run_id": "old"})
        assert explicit.json()["total_score"] == 61
        assert client.get(f"/api/v1/scores/{trading_date}").json()[0]["total_score"] == 82
        assert client.get(f"/api/v1/candidates/{trading_date}").json()[0]["total_score"] == 82
        assert client.get(f"/api/v1/portfolios/{trading_date}").json()["run_id"] == "new"
        assert client.get(f"/api/v1/reports/{trading_date}").json()["run_id"] == "new"
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_latest_fused_results_are_visible_without_reusing_old_portfolio() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    trading_date = date(2026, 7, 14)
    now = datetime(2026, 7, 14, 10, tzinfo=UTC)
    _add_result_set(session, "old-success", trading_date, now, "SUCCEEDED", 61)
    _add_result_set(session, "new-fused", trading_date, now + timedelta(minutes=1), "FUSED", 88)
    fused_run = session.get(JobRun, "new-fused")
    assert fused_run is not None
    fused_run.manifest = {
        "data_quality_gate": {
            "formal_eligible_count": 8,
            "formal_eligible_symbols": ["600000.SH"],
            "excluded_symbols": {"600001.SH": ["MISSING_OFFICIAL_DISCLOSURE"]},
        },
        "risk_outcome": {
            "state": "OBSERVE_ONLY",
            "reason_code": "INSUFFICIENT_COMPLETE_SYMBOLS",
            "reason_message": "正式可用股票仅 8 只，少于组合要求的 15 只",
        },
    }
    session.query(PortfolioRow).filter(PortfolioRow.run_id == "new-fused").delete()
    session.commit()

    _override_authenticated_admin(session)
    try:
        client = TestClient(app)
        assert client.get(f"/api/v1/scores/{trading_date}").json()[0]["total_score"] == 88
        assert client.get(f"/api/v1/candidates/{trading_date}").json()[0]["total_score"] == 88
        assert client.get(f"/api/v1/reports/{trading_date}").json()["run_id"] == "new-fused"
        portfolio = client.get(f"/api/v1/portfolios/{trading_date}")
        assert portfolio.status_code == 200
        assert portfolio.json()["run_id"] == "new-fused"
        assert portfolio.json()["observation_only"] is True
        assert portfolio.json()["positions"] == []
        assert portfolio.json()["reason_code"] == "INSUFFICIENT_COMPLETE_SYMBOLS"
        assert portfolio.json()["excluded_symbols"] == {
            "600001.SH": ["MISSING_OFFICIAL_DISCLOSURE"]
        }
        recent = client.get(
            "/api/v1/research/runs", params={"limit": 5, "trading_date": trading_date}
        ).json()
        assert recent[0]["status"] == "FUSED"
        assert recent[0]["progress"] == 100
        assert recent[0]["report_id"] == "report-new-fused"
        assert recent[0]["reason_code"] == "INSUFFICIENT_COMPLETE_SYMBOLS"
        assert recent[0]["formal_eligible_count"] == 8
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_published_research_runs_exclude_unpublished_and_order_by_completion() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime(2026, 7, 17, 10, tzinfo=UTC)
    _add_result_set(session, "older-success", date(2026, 7, 16), now, "SUCCEEDED", 70)
    _add_result_set(
        session, "latest-fused", date(2026, 7, 17), now + timedelta(minutes=2), "FUSED", 80
    )
    _add_result_set(
        session, "latest-failed", date(2026, 7, 17), now + timedelta(minutes=3), "FAILED", 90
    )
    session.commit()

    _override_authenticated_admin(session)
    try:
        client = TestClient(app)
        rows = client.get(
            "/api/v1/research/runs", params={"limit": 5, "published": True}
        ).json()
        assert [row["run_id"] for row in rows] == ["latest-fused", "older-success"]
        dated = client.get(
            "/api/v1/research/runs",
            params={"limit": 5, "published": True, "trading_date": "2026-07-17"},
        ).json()
        assert [row["run_id"] for row in dated] == ["latest-fused"]
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_failed_backtest_retry_is_single_queue_transition(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    session.add(
        JobRun(
            run_id="retry-snapshot-run",
            user_id="legacy-test-admin",
            run_type="DAILY",
            trading_date=date(2025, 12, 31),
            decision_at=now,
            status="SUCCEEDED",
            idempotency_key="retry-snapshot-key",
            manifest={},
            input_hash="1" * 64,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        SnapshotManifestRow(
            snapshot_id="retry-snapshot",
            run_id="retry-snapshot-run",
            dataset="backtest_bundle",
            source="fixture",
            schema_version="1",
            adapter_version="1",
            fetched_at=now,
            row_count=1,
            payload_sha256="2" * 64,
            parquet_uri="file:///retry.parquet",
            status="COMMITTED",
            details={"parquet_file_sha256": "3" * 64},
            committed_at=now,
        )
    )
    session.add(
        JobRun(
            run_id="retry-job",
            user_id="legacy-test-admin",
            run_type="BACKTEST",
            trading_date=date(2025, 12, 31),
            decision_at=now,
            status="FAILED",
            idempotency_key="retry-job-key",
            manifest={},
            input_hash="4" * 64,
            started_at=now,
            completed_at=now,
            error_message="old failure",
        )
    )
    session.add(
        BacktestRun(
            backtest_id="retry-backtest",
            run_id="retry-job",
            user_id="legacy-test-admin",
            name="retry",
            status="FAILED",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            config={"snapshot_file_hashes": {"retry-snapshot": "3" * 64}},
            snapshot_ids=["retry-snapshot"],
            metrics={"old": True},
            artifacts={"old": True},
            input_hash="4" * 64,
            output_hash="5" * 64,
            created_at=now,
            completed_at=now,
        )
    )
    session.commit()
    queued: list[str] = []
    monkeypatch.setattr("ashare_ai.api.app.read_backtest_bundle", lambda *_: None)
    monkeypatch.setattr("ashare_ai.api.app.enqueue_backtest", queued.append)
    _override_authenticated_admin(session)
    try:
        client = TestClient(app)
        first = client.post("/api/v1/backtests/retry-backtest/retry")
        second = client.post("/api/v1/backtests/retry-backtest/retry")
        assert first.status_code == 202
        assert first.json()["retry_count"] == 1
        assert first.json()["metrics"] is None
        assert second.status_code == 409
        assert queued == ["retry-backtest"]
        events = session.query(AuditEvent).filter(
            AuditEvent.run_id == "retry-job",
            AuditEvent.event_type == "BACKTEST_RETRY_REQUESTED",
        ).all()
        assert len(events) == 1
    finally:
        app.dependency_overrides.clear()
        session.close()


def _add_result_set(
    session: Session,
    run_id: str,
    trading_date: date,
    completed_at: datetime,
    status: str,
    total_score: float,
) -> None:
    session.add(
        JobRun(
            run_id=run_id,
            run_type="DAILY",
            trading_date=trading_date,
            decision_at=completed_at,
            status=status,
            idempotency_key=f"key-{run_id}",
            manifest={},
            input_hash=(run_id[0] * 64),
            output_hash=(run_id[-1] * 64),
            started_at=completed_at - timedelta(minutes=1),
            completed_at=completed_at,
        )
    )
    session.add(
        ScoreRow(
            run_id=run_id,
            symbol="600000.SH",
            trading_date=trading_date,
            decision_at=completed_at,
            fundamental_score=total_score,
            technical_score=total_score,
            sentiment_score=total_score,
            quality_confidence_score=total_score,
            total_score=total_score,
            formula_version="v1",
            agent_bundle_sha256="a" * 64,
            evidence_bundle_sha256="b" * 64,
            feature_snapshot_id=f"snapshot-{run_id}",
        )
    )
    session.add(
        CandidateRow(
            run_id=run_id,
            symbol="600000.SH",
            trading_date=trading_date,
            decision_at=completed_at,
            rank=1,
            total_score=total_score,
            prediction_percentile=0.9,
            industry_code="BANK",
            event_risk_multiplier=1.0,
            evidence_hash="c" * 64,
        )
    )
    session.add(
        PortfolioRow(
            portfolio_id=f"portfolio-{run_id}",
            run_id=run_id,
            trading_date=trading_date,
            effective_trading_date=trading_date + timedelta(days=1),
            status=status,
            expected_turnover=0.1,
            cash_weight=0.2,
            constraint_version="v1",
            input_hash="d" * 64,
            positions=[],
            rejection_reasons=[],
        )
    )
    session.add(
        ReportRow(
            report_id=f"report-{run_id}",
            run_id=run_id,
            trading_date=trading_date,
            report_type="DAILY_RESEARCH",
            object_uri=f"s3://reports/{run_id}",
            content_sha256="e" * 64,
            created_at=completed_at,
        )
    )
