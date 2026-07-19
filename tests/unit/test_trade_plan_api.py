from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import AuthContext
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.storage.models import (
    Base,
    CandidateRow,
    JobRun,
    ReportRow,
    ScoreRow,
    SnapshotManifestRow,
    TradePlanRow,
    UserAccount,
)


def _client(session: Session, user_id: str = "user-1") -> TestClient:
    def override_db():
        yield session

    context = AuthContext(
        user=cast(
            Any,
            SimpleNamespace(
                user_id=user_id,
                username="fixture",
                role="USER",
                enabled=True,
            ),
        ),
        session=cast(Any, SimpleNamespace()),
    )
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_auth_context] = lambda: context
    app.dependency_overrides[get_write_context] = lambda: context
    return TestClient(app)


def _database() -> tuple[Session, datetime]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False), datetime.now(UTC)


def test_research_settings_are_per_user_and_default_off() -> None:
    session, now = _database()
    session.add(
        UserAccount(
            user_id="user-1",
            username="fixture",
            password_hash="hash",
            role="USER",
            enabled=True,
            session_version=1,
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()
    client = _client(session)
    try:
        initial = client.get("/api/v1/research/settings")
        assert initial.status_code == 200
        assert initial.json()["auto_enabled"] is False
        assert initial.json()["snapshot_mode"] == "SYSTEM_ENFORCED"
        updated = client.put("/api/v1/research/settings", json={"auto_enabled": True})
        assert updated.status_code == 200
        assert updated.json()["auto_enabled"] is True
        assert updated.json()["automatic_total_budget"] == "1000000"
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_trade_plan_submission_binds_report_run_and_reuses_active_request(monkeypatch) -> None:
    session, now = _database()
    run = JobRun(
        run_id="run-1",
        user_id="user-1",
        run_type="DAILY",
        trading_date=date(2026, 7, 15),
        decision_at=now,
        status="SUCCEEDED",
        idempotency_key="run-key",
        manifest={
            "policy_version": "first-release-v2",
            "data_quality_gate": {"passed": True},
            "model_configuration": {"enabled": False},
        },
        input_hash="a" * 64,
        started_at=now,
        completed_at=now,
    )
    report = ReportRow(
        report_id="report-1",
        run_id=run.run_id,
        trading_date=run.trading_date,
        report_type="DAILY_RESEARCH",
        object_uri="file:///report",
        content_sha256="b" * 64,
        created_at=now,
    )
    candidate = CandidateRow(
        run_id=run.run_id,
        symbol="600000.SH",
        trading_date=run.trading_date,
        decision_at=now,
        rank=1,
        total_score=80,
        prediction_percentile=1,
        industry_code="BANK",
        event_risk_multiplier=1,
        style_exposures={},
        evidence_hash="c" * 64,
    )
    snapshot = SnapshotManifestRow(
        snapshot_id="snapshot-1",
        run_id=run.run_id,
        dataset="backtest_bundle",
        source="fixture",
        schema_version="1",
        adapter_version="1",
        fetched_at=now,
        row_count=10,
        payload_sha256="d" * 64,
        parquet_uri="file:///snapshot",
        status="COMMITTED",
        details={
            "parquet_file_sha256": "e" * 64,
            "future_trading_dates": ["2026-07-16", "2026-07-17", "2026-07-20"],
        },
        committed_at=now,
    )
    session.add_all([run, report, candidate, snapshot])
    session.commit()
    queued: list[str] = []
    monkeypatch.setattr("ashare_ai.api.app.enqueue_trade_plan", queued.append)
    client = _client(session)
    try:
        response = client.post(
            "/api/v1/reports/report-1/trade-plans",
            json={"symbols": ["600000.SH"], "budget_override": 100000},
        )
        assert response.status_code == 202
        assert response.json()["run_id"] == "run-1"
        assert response.json()["snapshot_ids"] == ["snapshot-1"]
        duplicate = client.post(
            "/api/v1/reports/report-1/trade-plans",
            json={"symbols": ["600000.SH"], "budget_override": 100000},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["plan_id"] == response.json()["plan_id"]
        assert len(queued) == 1
        stored = session.get(TradePlanRow, response.json()["plan_id"])
        assert stored is not None
        assert stored.request_payload["optimizer_policy"]["training_sessions"] == 160
        assert stored.request_payload["optimizer_policy"]["validation_sessions"] == 80
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_trade_plan_rejects_fused_and_critical_candidates(monkeypatch) -> None:
    session, now = _database()
    run = JobRun(
        run_id="run-2",
        user_id="user-1",
        run_type="DAILY",
        trading_date=date(2026, 7, 15),
        decision_at=now,
        status="FUSED",
        idempotency_key="run-key-2",
        manifest={},
        input_hash="a" * 64,
        started_at=now,
        completed_at=now,
    )
    session.add_all(
        [
            run,
            ReportRow(
                report_id="report-2",
                run_id=run.run_id,
                trading_date=run.trading_date,
                report_type="DAILY_RESEARCH",
                object_uri="file:///report",
                content_sha256="b" * 64,
                created_at=now,
            ),
            CandidateRow(
                run_id=run.run_id,
                symbol="600000.SH",
                trading_date=run.trading_date,
                decision_at=now,
                rank=1,
                total_score=0,
                prediction_percentile=1,
                industry_code="BANK",
                event_risk_multiplier=0,
                style_exposures={},
                evidence_hash="c" * 64,
            ),
        ]
    )
    session.commit()
    monkeypatch.setattr("ashare_ai.api.app.enqueue_trade_plan", lambda _: None)
    client = _client(session)
    try:
        response = client.post(
            "/api/v1/reports/report-2/trade-plans",
            json={"symbols": ["600000.SH"]},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "GLOBAL_RISK_FUSE_ACTIVE"
        run.status = "SUCCEEDED"
        run.manifest = {"data_quality_gate": {"formal_eligible_symbols": ["600000.SH"]}}
        session.commit()
        critical = client.post(
            "/api/v1/reports/report-2/trade-plans",
            json={"symbols": ["600000.SH"]},
        )
        assert critical.status_code == 409
        assert critical.json()["detail"]["code"] == "CRITICAL_EVENT_RISK"
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_report_symbols_include_data_limited_stock_as_no_buy() -> None:
    session, now = _database()
    run = JobRun(
        run_id="run-symbols",
        user_id="user-1",
        run_type="DAILY",
        trading_date=date(2026, 7, 15),
        decision_at=now,
        status="SUCCEEDED",
        idempotency_key="run-symbols-key",
        manifest={
            "target_symbols": ["600000.SH", "600001.SH"],
            "data_quality_gate": {
                "formal_eligible_symbols": ["600000.SH"],
                "excluded_symbols": {"600001.SH": ["MISSING_OFFICIAL_DISCLOSURE"]},
            },
        },
        input_hash="a" * 64,
        started_at=now,
        completed_at=now,
    )
    report = ReportRow(
        report_id="report-symbols",
        run_id=run.run_id,
        trading_date=run.trading_date,
        report_type="DAILY_RESEARCH",
        object_uri="file:///report",
        content_sha256="b" * 64,
        created_at=now,
    )
    scores = [
        ScoreRow(
            run_id=run.run_id,
            symbol=symbol,
            trading_date=run.trading_date,
            decision_at=now,
            fundamental_score=70,
            technical_score=70,
            sentiment_score=70,
            quality_confidence_score=70,
            base_total_score=70,
            dividend_bonus=0,
            event_risk_multiplier=1,
            total_score=70,
            formula_version="v2",
            agent_bundle_sha256="c" * 64,
            evidence_bundle_sha256="d" * 64,
            feature_snapshot_id="feature",
        )
        for symbol in ("600000.SH", "600001.SH")
    ]
    candidate = CandidateRow(
        run_id=run.run_id,
        symbol="600000.SH",
        trading_date=run.trading_date,
        decision_at=now,
        rank=1,
        total_score=70,
        prediction_percentile=1,
        industry_code="BANK",
        event_risk_multiplier=1,
        style_exposures={},
        evidence_hash="e" * 64,
    )
    session.add_all([run, report, *scores, candidate])
    session.commit()
    client = _client(session)
    try:
        response = client.get("/api/v1/reports/report-symbols/symbols")
        assert response.status_code == 200
        rows = {item["symbol"]: item for item in response.json()}
        assert rows["600000.SH"]["advice_eligible"] is True
        assert rows["600001.SH"]["recommendation"] == "NO_BUY"
        assert rows["600001.SH"]["exclusion_reasons"] == ["MISSING_OFFICIAL_DISCLOSURE"]
        other_client = _client(session, user_id="other-user")
        assert other_client.get("/api/v1/reports/report-symbols/symbols").status_code == 404
        assert other_client.post(
            "/api/v1/reports/report-symbols/trade-plans",
            json={"symbols": ["600000.SH"]},
        ).status_code == 404
    finally:
        app.dependency_overrides.clear()
        session.close()
