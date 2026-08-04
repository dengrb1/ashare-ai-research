from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import AuthContext
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.api.run_cleanup import delete_run
from ashare_ai.storage.models import (
    AgentCall,
    AuditEvent,
    BacktestRun,
    Base,
    BuyEntryMonitorRow,
    CandidateRow,
    EvidenceRow,
    ExitAdviceRow,
    JobRun,
    NotificationRow,
    PortfolioRow,
    ReportRow,
    ScoreRow,
    SnapshotManifestRow,
    TradePlanRow,
    UserAccount,
)


def _engine():
    return create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _now() -> datetime:
    return datetime(2026, 7, 20, 10, tzinfo=UTC)


def _add_run(session: Session, run_id: str) -> JobRun:
    now = _now()
    run = JobRun(
        run_id=run_id,
        user_id="cleanup-user",
        run_type="DAILY",
        trading_date=date(2026, 7, 20),
        decision_at=now,
        status="SUCCEEDED",
        idempotency_key=f"cleanup-{run_id}",
        manifest={"run_id": run_id},
        input_hash="a" * 64,
        started_at=now,
        completed_at=now,
    )
    session.add(run)
    session.flush()
    return run


def _seed_derived(session: Session, run_id: str) -> tuple[str, str]:
    now = _now()
    session.add(
        AuditEvent(
            run_id=run_id, event_type="STARTED", severity="INFO", message="start", details={},
            created_at=now,
        )
    )
    session.add(
        AgentCall(
            run_id=run_id, symbol="600519.SH", component="fundamental", model_provider="fixture",
            model_name="fixture-model", reasoning_effort="medium", input_tokens=1, output_tokens=1,
            cache_policy="COMPATIBLE", duration_ms=1, retry_count=0, result_status="SUCCEEDED",
            request_sha256="b" * 64, response_sha256="c" * 64, result={}, created_at=now,
        )
    )
    session.add(
        EvidenceRow(
            run_id=run_id, symbol="600519.SH", component="fundamental", evidence_id="e-1",
            evidence_type="quote", source="fixture", source_record_id="r-1", available_at=now,
            payload_sha256="d" * 64, excerpt="excerpt",
        )
    )
    session.add(
        ScoreRow(
            run_id=run_id, symbol="600519.SH", trading_date=date(2026, 7, 20), decision_at=now,
            fundamental_score=1, technical_score=1, sentiment_score=1, quality_confidence_score=1,
            total_score=4, formula_version="v1", agent_bundle_sha256="e" * 64,
            evidence_bundle_sha256="f" * 64, feature_snapshot_id="snap-feature",
        )
    )
    session.add(
        CandidateRow(
            run_id=run_id, symbol="600519.SH", trading_date=date(2026, 7, 20), decision_at=now,
            rank=1, total_score=4, prediction_percentile=90, industry_code="LS",
            industry_name="白酒", event_risk_multiplier=1, style_exposures={},
            evidence_hash="g" * 64,
        )
    )
    session.add(
        PortfolioRow(
            run_id=run_id, trading_date=date(2026, 7, 20), effective_trading_date=date(2026, 7, 20),
            status="SUCCEEDED", expected_turnover=0.1, cash_weight=0.5, constraint_version="v1",
            input_hash="h" * 64, positions=[], rejection_reasons=[],
        )
    )
    report = ReportRow(
        report_id="report-clean-1", run_id=run_id, trading_date=date(2026, 7, 20),
        report_type="DAILY", object_uri="lake://reports/report-clean-1.parquet",
        content_sha256="i" * 64, created_at=now,
    )
    session.add(report)
    session.flush()
    plan = TradePlanRow(
        plan_id="plan-clean-1", user_id="cleanup-user", report_id=report.report_id, run_id=run_id,
        operation_run_id=run_id, trading_date=date(2026, 7, 20), decision_at=now, available_at=now,
        status="SUCCEEDED", objective="RISK_ADJUSTED_RETURN", symbols=["600519.SH"],
        budget_override=Decimal("100000"), request_payload={}, snapshot_ids=[],
        optimizer_version="v1", config_version="v1", input_hash="j" * 64, output_hash="k" * 64,
        created_at=now,
    )
    session.add(plan)
    session.flush()
    session.add(
        BuyEntryMonitorRow(
            user_id="cleanup-user", symbol="600519.SH", status="ACTIVE",
            effective_date=date(2026, 7, 21), expires_at=_now(), entry_low=Decimal("1400"),
            entry_high=Decimal("1500"), score_run_id=run_id, trade_plan_id=plan.plan_id,
            rationale={}, created_at=now, updated_at=now,
        )
    )
    session.add(
        ExitAdviceRow(
            user_id="cleanup-user", operation_run_id=run_id, symbol="600519.SH", status="SUCCEEDED",
            decision_at=now, available_at=now, current_price=Decimal("1450"),
            unrealized_profit=Decimal("50"), trigger_amount=Decimal("1400"),
            trigger_type="PROFIT_AMOUNT", position_snapshot={}, research_context={},
            prompt_version="v1", input_hash="o" * 64, created_at=now,
        )
    )
    session.add(
        NotificationRow(
            user_id="cleanup-user", notification_type="RUN", severity="INFO", title="运行完成",
            body="已删除", payload={}, resource_type="RUN", resource_id=run_id, created_at=now,
            expires_at=now,
        )
    )
    return report.report_id, plan.plan_id


def _counts(session: Session, run_id: str) -> dict[str, int]:
    def n(model: type[Any]) -> int:
        return session.query(model).filter(model.run_id == run_id).count()

    by_run = {
        "run": JobRun,
        "audit": AuditEvent,
        "agent": AgentCall,
        "evidence": EvidenceRow,
        "score": ScoreRow,
        "candidate": CandidateRow,
        "portfolio": PortfolioRow,
        "report": ReportRow,
        "plan": TradePlanRow,
    }
    counts = {key: n(model) for key, model in by_run.items()}
    counts["monitor"] = (
        session.query(BuyEntryMonitorRow).filter(BuyEntryMonitorRow.score_run_id == run_id).count()
    )
    counts["exit"] = (
        session.query(ExitAdviceRow).filter(ExitAdviceRow.operation_run_id == run_id).count()
    )
    counts["notification"] = (
        session.query(NotificationRow).filter(NotificationRow.resource_id == run_id).count()
    )
    return counts


def test_delete_run_cascades_logs_and_results_but_preserves_lake_objects() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    now = _now()
    with Session(engine) as session:
        session.add(
            UserAccount(
                user_id="cleanup-user", username="cleanup-user", password_hash="hash",
                role="USER", enabled=True, session_version=1, created_at=now, updated_at=now,
            )
        )
        run = _add_run(session, "run-clean-1")
        report_id, plan_id = _seed_derived(session, run.run_id)

        snapshot = SnapshotManifestRow(
            snapshot_id="snap-lake-1", run_id=run.run_id, dataset="daily", source="fixture",
            schema_version="1", adapter_version="1", fetched_at=now, row_count=1,
            payload_sha256="l" * 64, parquet_uri="lake://snapshots/snap-lake-1.parquet",
            status="COMMITTED", details={}, committed_at=now,
        )
        session.add(snapshot)
        backtest = BacktestRun(
            backtest_id="backtest-clean-1", user_id="cleanup-user", run_id=run.run_id,
            name="历史回测", status="SUCCEEDED", start_date=date(2026, 1, 1),
            end_date=date(2026, 7, 1), config={}, snapshot_ids=[], metrics={},
            input_hash="m" * 64, output_hash="n" * 64, retry_count=0, created_at=now,
            completed_at=now,
        )
        session.add(backtest)
        session.commit()

        delete_run(session, run)
        session.commit()

        assert _counts(session, run.run_id) == {
            "run": 0, "audit": 0, "agent": 0, "evidence": 0, "score": 0, "candidate": 0,
            "portfolio": 0, "report": 0, "plan": 0, "monitor": 0, "exit": 0, "notification": 0,
        }
        # The immutable lake snapshot and backtest record survive; the run
        # reference is detached rather than deleted.
        assert session.get(SnapshotManifestRow, "snap-lake-1") is not None
        assert session.get(SnapshotManifestRow, "snap-lake-1").run_id is None
        assert session.get(BacktestRun, "backtest-clean-1") is not None
        assert session.get(BacktestRun, "backtest-clean-1").run_id is None
        assert session.get(TradePlanRow, plan_id) is None
        assert session.get(ReportRow, report_id) is None


def test_delete_run_detaches_report_owned_notifications_for_the_run() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    now = _now()
    with Session(engine) as session:
        session.add(
            UserAccount(
                user_id="cleanup-user", username="cleanup-user", password_hash="hash",
                role="USER", enabled=True, session_version=1, created_at=now, updated_at=now,
            )
        )
        run = _add_run(session, "run-clean-2")
        _, plan_id = _seed_derived(session, run.run_id)
        # A notification referencing the trade plan (not the run directly).
        session.add(
            NotificationRow(
                user_id="cleanup-user", notification_type="TRADE_PLAN", severity="INFO",
                title="买入方案", body="body", payload={}, resource_type="TRADE_PLAN",
                resource_id=plan_id, created_at=now, expires_at=now,
            )
        )
        session.commit()

        delete_run(session, run)
        session.commit()

        assert _counts(session, run.run_id)["notification"] == 0
        assert (
            session.query(NotificationRow).filter(NotificationRow.resource_id == plan_id).count()
            == 0
        )


def _context(user: UserAccount) -> AuthContext:
    return AuthContext(user=cast(Any, user), session=cast(Any, SimpleNamespace()))


def test_run_delete_and_clear_completed_endpoints_enforce_ownership_and_status(monkeypatch) -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    now = _now()
    session = Session(engine, expire_on_commit=False)
    owner = UserAccount(
        user_id="cleanup-owner", username="cleanup-owner", password_hash="hash", role="USER",
        enabled=True, session_version=1, created_at=now, updated_at=now,
    )
    admin = UserAccount(
        user_id="cleanup-admin", username="cleanup-admin", password_hash="hash", role="ADMIN",
        enabled=True, session_version=1, created_at=now, updated_at=now,
    )
    session.add_all((owner, admin))
    session.commit()

    terminal_run = _add_run(session, "run-api-terminal")
    terminal_run.user_id = owner.user_id
    active_run = _add_run(session, "run-api-active")
    active_run.user_id = owner.user_id
    active_run.status = "RUNNING"
    admin_terminal = _add_run(session, "run-api-admin-terminal")
    admin_terminal.user_id = admin.user_id
    session.commit()

    contexts = [_context(owner)]

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_auth_context] = lambda: contexts[0]
    app.dependency_overrides[get_write_context] = lambda: contexts[0]
    try:
        client = TestClient(app)
        # Non-admin cannot clear completed runs and cannot delete another run.
        assert client.post("/api/v1/runs/clear-completed", json={}).status_code == 403
        assert client.delete(f"/api/v1/runs/{active_run.run_id}").status_code == 409
        assert client.delete(f"/api/v1/runs/{terminal_run.run_id}").status_code == 204
        assert session.get(JobRun, terminal_run.run_id) is None
        # Idempotent deletion: a second delete returns 404, not an error.
        assert client.delete(f"/api/v1/runs/{terminal_run.run_id}").status_code == 404

        # An active run survives bulk clear-completed.
        clear = client.post(
            "/api/v1/runs/clear-completed",
            headers={"Idempotency-Key": "clear-once"},
            json={"before": None},
        )
        assert clear.status_code == 403  # still non-admin

        contexts[0] = _context(admin)
        assert (
            client.post("/api/v1/runs/clear-completed", json={}).status_code == 400  # key required
        )
        cleared = client.post(
            "/api/v1/runs/clear-completed",
            headers={"Idempotency-Key": "clear-once"},
            json={"before": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["deleted"] == 1
        assert session.get(JobRun, active_run.run_id) is not None  # active run untouched
        replay = client.post(
            "/api/v1/runs/clear-completed",
            headers={"Idempotency-Key": "clear-once"},
            json={"before": None},
        )
        assert replay.status_code == 200
    finally:
        app.dependency_overrides.clear()
        session.close()
