from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.config import Settings
from ashare_ai.orchestration.akshare_bundle import BenchmarkDataNotReadyError
from ashare_ai.orchestration.backtest_snapshot import create_backtest_snapshot
from ashare_ai.orchestration.builtin import BuiltinDailyBackend
from ashare_ai.orchestration.builtin_backtest import read_backtest_bundle
from ashare_ai.orchestration.bundle import make_demo_bundle
from ashare_ai.orchestration.daily import daily_research_flow
from ashare_ai.orchestration.production import ApplicationPipeline
from ashare_ai.storage.models import Base, JobRun, SnapshotManifestRow


def test_daily_research_builds_cumulative_executable_backtest_snapshot(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CANONICAL_BUNDLE_MODE", "demo")
    monkeypatch.setenv("ALLOW_DEMO_DATA", "true")
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    settings = Settings(
        canonical_bundle_mode="demo",
        allow_demo_data=True,
        lake_root=tmp_path / "lake",
        agent_backend="builtin",
    )

    def pipeline() -> ApplicationPipeline:
        backend = BuiltinDailyBackend(
            session_factory=factory,
            object_root=tmp_path / "objects",
            state_root=tmp_path / "state",
            policy_path="configs/first_release.v1.json",
            allow_demo_data=True,
        )
        backend._settings = settings
        return ApplicationPipeline(backend, session_factory=factory)

    prior_date = date(2026, 7, 13)
    daily_research_flow(prior_date, pipeline())
    daily_research_flow(date(2026, 7, 14), pipeline())
    with factory() as session:
        snapshots = list(
            session.scalars(
                select(SnapshotManifestRow)
                .where(SnapshotManifestRow.dataset == "backtest_bundle")
                .order_by(SnapshotManifestRow.fetched_at)
            )
        )
        assert snapshots
        latest = snapshots[-1]
        assert latest.status == "COMMITTED"
        assert latest.details["calendar_start"] < latest.details["calendar_end"]
        assert latest.details["signal_count"] >= 30
        assert latest.details["executable_signal_count"] >= 15
        assert latest.details["prior_snapshot_id"] is not None
        bundle = read_backtest_bundle(
            {latest.snapshot_id: latest.parquet_uri},
            {latest.snapshot_id: latest.details["parquet_file_sha256"]},
        )
        assert any(signal.signal_date < bundle.trading_calendar[-1] for signal in bundle.signals)
        assert all(bar.price_basis == "RAW" for bar in bundle.bars)
        assert latest.details["research_price_basis"] == "RAW"
        assert latest.details["execution_price_basis"] == "RAW"
        canonical = list(
            session.scalars(
                select(SnapshotManifestRow).where(
                    SnapshotManifestRow.run_id == latest.run_id,
                    SnapshotManifestRow.dataset.in_(
                        (
                            "canonical_news",
                            "canonical_cash_dividends",
                            "canonical_trading_calendar",
                        )
                    ),
                )
            )
        )
        assert {item.dataset for item in canonical} == {
            "canonical_news",
            "canonical_cash_dividends",
            "canonical_trading_calendar",
        }
        assert all(item.status == "COMMITTED" for item in canonical)


def test_observe_only_research_still_builds_labeled_research_backtest_snapshot(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CANONICAL_BUNDLE_MODE", "demo")
    monkeypatch.setenv("ALLOW_DEMO_DATA", "true")
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    settings = Settings(
        canonical_bundle_mode="demo",
        allow_demo_data=True,
        lake_root=tmp_path / "lake",
        agent_backend="builtin",
    )

    def pipeline() -> ApplicationPipeline:
        backend = BuiltinDailyBackend(
            session_factory=factory,
            object_root=tmp_path / "objects",
            state_root=tmp_path / "state",
            policy_path="configs/first_release.v1.json",
            allow_demo_data=True,
        )
        backend._settings = settings
        return ApplicationPipeline(backend, session_factory=factory)

    def run_observe_only(value: date) -> None:
        current = pipeline()
        run_id = current.start_run(value)
        current.sync_reference_data(run_id)
        snapshots = current.ingest_and_verify(run_id)
        universe = current.build_universe(run_id, snapshots)
        features = current.build_features(run_id, universe)
        agents = current.run_research_agents(run_id, features)
        scores = current.calculate_scores(run_id, agents)
        current.qlib_filter(run_id, scores)
        report_id = current.publish_report(run_id, None, "OBSERVE_ONLY")
        current.complete_run(run_id, report_id, "FUSED")

    run_observe_only(date(2026, 7, 13))
    run_observe_only(date(2026, 7, 14))
    with factory() as session:
        rows = list(
            session.scalars(
                select(SnapshotManifestRow)
                .where(SnapshotManifestRow.dataset == "backtest_bundle")
                .order_by(SnapshotManifestRow.fetched_at)
            )
        )
        assert len(rows) == 2
        assert rows[-1].details["observe_only"] is True
        assert rows[-1].details["run_status"] == "FUSED"
        assert rows[-1].details["prior_snapshot_id"] == rows[0].snapshot_id
        assert rows[-1].details["executable_signal_count"] >= 15


def test_historical_rules_do_not_backfill_current_st_status(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    trading_date = date(2026, 7, 14)
    source = make_demo_bundle(trading_date)
    bundle = source.model_copy(
        update={
            "statuses": tuple(
                item.model_copy(
                    update={"is_st": True, "is_suspended": index == 0}
                )
                for index, item in enumerate(source.statuses)
            )
        }
    )
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        allow_demo_data=True,
    )
    with factory() as session:
        run = JobRun(
            run_id="pit-rule-run",
            run_type="DAILY",
            trading_date=trading_date,
            decision_at=bundle.decision_at,
            status="RUNNING",
            idempotency_key="pit-rule-key",
            manifest={},
            input_hash="7" * 64,
            started_at=bundle.decision_at,
        )
        session.add(run)
        session.flush()
        row = create_backtest_snapshot(
            session=session,
            run=run,
            bundle=bundle,
            lake_root=tmp_path / "lake",
            policy=backend.policy,
            dataset="backtest_bundle",
        )
        session.commit()
    frozen = read_backtest_bundle(
        {row.snapshot_id: row.parquet_uri},
        {row.snapshot_id: row.details["parquet_file_sha256"]},
    )
    symbol = bundle.securities[0].symbol
    rules = {
        item.trading_date: item.rule
        for item in frozen.rules
        if item.symbol == symbol
    }
    assert rules[trading_date].price_limit_ratio == Decimal("0.05")
    prior_date = max(value for value in rules if value < trading_date)
    assert rules[prior_date].price_limit_ratio == Decimal("0.10")
    assert all(item.rule.available_at <= bundle.decision_at for item in frozen.rules)
    current_bar = next(
        item
        for item in frozen.bars
        if item.symbol == symbol and item.trading_date == trading_date
    )
    assert current_bar.trade_status.value == "SUSPENDED"


def test_backtest_snapshot_reports_missing_benchmark_calendar_coverage(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    trading_date = date(2026, 7, 21)
    source = make_demo_bundle(trading_date)
    incomplete_csi500 = dict(source.benchmark_returns["CSI500"])
    del incomplete_csi500[trading_date]
    bundle = source.model_copy(
        update={
            "benchmark_returns": {
                **source.benchmark_returns,
                "CSI500": incomplete_csi500,
            }
        }
    )
    backend = BuiltinDailyBackend(
        session_factory=sessionmaker(bind=engine, class_=Session, expire_on_commit=False),
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        allow_demo_data=True,
    )
    with Session(engine) as session:
        run = JobRun(
            run_id="incomplete-benchmark-run",
            run_type="DAILY",
            trading_date=trading_date,
            decision_at=bundle.decision_at,
            status="RUNNING",
            idempotency_key="incomplete-benchmark-key",
            manifest={},
            input_hash="8" * 64,
            started_at=bundle.decision_at,
        )
        session.add(run)
        session.flush()
        try:
            create_backtest_snapshot(
                session=session,
                run=run,
                bundle=bundle,
                lake_root=tmp_path / "lake",
                policy=backend.policy,
            )
        except BenchmarkDataNotReadyError as exc:
            assert exc.missing_benchmarks == ("CSI500",)
            assert exc.last_available_dates == {"CSI500": date(2026, 7, 20)}
            assert exc.audit_details()["missing_date_summary"]["CSI500"] == {
                "count": 1,
                "first": "2026-07-21",
                "last": "2026-07-21",
            }
        else:
            raise AssertionError("snapshot must fail closed on missing required benchmark date")
