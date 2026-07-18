from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.backtest.trade_plan import TradePlanOutcome
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.orchestration.builtin import BuiltinDailyBackend
from ashare_ai.orchestration.daily import daily_research_flow
from ashare_ai.orchestration.production import ApplicationPipeline
from ashare_ai.orchestration.trade_plan_jobs import execute_trade_plan_job
from ashare_ai.storage.models import (
    Base,
    CandidateRow,
    SnapshotManifestRow,
    TradePlanRow,
    UserAccount,
)


def test_trade_plan_worker_reads_committed_raw_snapshot_and_degrades_without_ai(
    tmp_path, monkeypatch
) -> None:
    lake_root = tmp_path / "lake"
    monkeypatch.setenv("CANONICAL_BUNDLE_MODE", "demo")
    monkeypatch.setenv("ALLOW_DEMO_DATA", "true")
    monkeypatch.setenv("AGENT_BACKEND", "builtin")
    monkeypatch.setenv("LAKE_ROOT", str(lake_root))
    get_settings.cache_clear()
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    settings = Settings(
        canonical_bundle_mode="demo",
        allow_demo_data=True,
        lake_root=lake_root,
        agent_backend="builtin",
    )
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v2.json",
        allow_demo_data=True,
    )
    backend._settings = settings
    result = daily_research_flow(
        date(2026, 7, 15), ApplicationPipeline(backend, session_factory=factory)
    )

    with factory() as session:
        snapshot = session.scalar(
            select(SnapshotManifestRow).where(
                SnapshotManifestRow.run_id == result["run_id"],
                SnapshotManifestRow.dataset == "backtest_bundle",
            )
        )
        candidate = session.scalar(
            select(CandidateRow)
            .where(CandidateRow.run_id == result["run_id"])
            .order_by(CandidateRow.rank)
        )
        assert snapshot is not None
        assert snapshot.status == "COMMITTED"
        assert snapshot.details["execution_price_basis"] == "RAW"
        assert candidate is not None
        now = candidate.decision_at
        session.add(
            UserAccount(
                user_id="worker-user",
                username="worker-user",
                password_hash="fixture",
                role="USER",
                enabled=True,
                session_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            TradePlanRow(
                plan_id="trade-plan-worker-test",
                user_id="worker-user",
                report_id=result["report_id"],
                run_id=result["run_id"],
                trading_date=candidate.trading_date,
                decision_at=now,
                available_at=now,
                status="PENDING",
                objective="RISK_ADJUSTED_RETURN",
                symbols=[candidate.symbol],
                budget_override=Decimal("100000"),
                request_payload={
                    "optimizer_policy": {
                        "history_sessions": 240,
                        "training_sessions": 160,
                        "validation_sessions": 80,
                        "entry_step_sessions": 10,
                    }
                },
                snapshot_ids=[snapshot.snapshot_id],
                optimizer_version="trade-plan-grid-v1",
                config_version="first-release-v2",
                prompt_version="trade-plan-explanation-v1",
                model_configuration={"enabled": False},
                input_hash="a" * 64,
                active_trade_plan_key="b" * 64,
                created_at=now,
            )
        )
        session.commit()

    try:
        plan = execute_trade_plan_job(
            "trade-plan-worker-test", session_factory=factory
        )
    finally:
        get_settings.cache_clear()

    assert plan.outcome in {TradePlanOutcome.BUY, TradePlanOutcome.NO_BUY}
    with factory() as session:
        stored = session.get(TradePlanRow, "trade-plan-worker-test")
        assert stored is not None
        assert stored.status == "SUCCEEDED"
        assert stored.deterministic_result is not None
        assert stored.ai_explanation == {
            "status": "UNAVAILABLE",
            "message": "AI解释未生成",
        }
        assert stored.output_hash is not None
        assert stored.object_sha256 is not None
