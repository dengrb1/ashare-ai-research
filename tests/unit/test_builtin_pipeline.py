from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ashare_ai.agents.openai_compatible import OpenAICompatibleError
from ashare_ai.agents.protocols import GenerationMetadata, StructuredGeneration
from ashare_ai.agents.validation import ComponentAnalysis
from ashare_ai.core.config import get_settings
from ashare_ai.core.time import SHANGHAI, market_decision_time
from ashare_ai.orchestration.builtin import BuiltinDailyBackend, FeatureArtifact
from ashare_ai.orchestration.bundle import CanonicalDailyBundle, make_demo_bundle
from ashare_ai.orchestration.daily import daily_research_flow
from ashare_ai.orchestration.production import ApplicationPipeline
from ashare_ai.portfolio.events import ActiveEventRisk, EventSeverity
from ashare_ai.storage.models import (
    AgentCall,
    AIResponseCacheRow,
    AuditEvent,
    Base,
    CandidateRow,
    JobRun,
    PortfolioRow,
    ReportRow,
    ScoreRow,
    SnapshotManifestRow,
    UserAccount,
)
from ashare_ai.universe.builder import UniverseResult


@pytest.fixture(autouse=True)
def _use_builtin_agents_in_tests(monkeypatch: pytest.MonkeyPatch):
    """Keep deterministic pipeline tests independent of a developer's local LLM settings."""
    monkeypatch.setenv("AGENT_BACKEND", "builtin")
    monkeypatch.setenv("CANONICAL_BUNDLE_MODE", "demo")
    monkeypatch.setenv("ALLOW_DEMO_DATA", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeStructuredLLMClient:
    def __init__(self, *, future_evidence: bool = False) -> None:
        self.future_evidence = future_evidence
        self.requests: list[dict[str, Any]] = []
        self.messages: list[tuple[Mapping[str, str], ...]] = []

    async def generate_structured(
        self,
        *,
        schema: type[BaseModel],
        messages: tuple[Mapping[str, str], ...],
        idempotency_key: str,
    ) -> StructuredGeneration:
        assert schema is ComponentAnalysis
        self.messages.append(messages)
        request = json.loads(messages[1]["content"])
        self.requests.append(request)
        evidence = dict(request["evidence"][0])
        if self.future_evidence:
            evidence["available_at"] = (
                datetime.fromisoformat(request["decision_at"]) + timedelta(seconds=1)
            ).isoformat()
        scores = {"fundamental": 71.0, "technical": 62.0, "sentiment": 53.0}
        return StructuredGeneration(
            output={
                "component": request["component"],
                "score": scores[request["component"]],
                "confidence": 0.75,
                "evidence": [evidence],
                "positive_factors": ["测试用的中文证据要点"],
                "negative_factors": [],
                "risk_flags": [],
            },
            metadata=GenerationMetadata(
                provider="fake-provider",
                model_name="fake-model",
                reasoning_effort="high",
                input_tokens=12,
                output_tokens=8,
                duration_ms=3,
                retry_count=0,
            ),
        )


class ConcurrentFakeStructuredLLMClient(FakeStructuredLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.maximum_active = 0

    async def generate_structured(self, **kwargs: Any) -> StructuredGeneration:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().generate_structured(**kwargs)
        finally:
            self.active -= 1


class UnavailableStructuredLLMClient:
    async def generate_structured(self, **kwargs: Any) -> StructuredGeneration:
        del kwargs
        raise OpenAICompatibleError(
            "Responses API request failed with status 403",
            code="MODEL_RESPONSE_ERROR",
            status_code=403,
        )


def test_run_context_normalizes_postgres_utc_timestamp_to_shanghai(tmp_path) -> None:
    run = SimpleNamespace(
        trading_date=date(2026, 7, 16),
        decision_at=datetime(2026, 7, 16, 10, tzinfo=UTC),
        manifest={"source": "test"},
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback

        def get(self, model, run_id):
            del model, run_id
            return run

    backend = BuiltinDailyBackend(
        session_factory=FakeSession,  # type: ignore[arg-type]
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
    )

    trading_date, decision_at, manifest = backend._run_context("run-id")

    assert trading_date == date(2026, 7, 16)
    assert decision_at == datetime(2026, 7, 16, 18, tzinfo=SHANGHAI)
    assert manifest == {"source": "test"}


def test_v2_scoring_reclassifies_legacy_generic_risk_event(tmp_path, monkeypatch) -> None:
    trading_date = date(2026, 7, 15)
    base = make_demo_bundle(trading_date)
    original = base.disclosures[0]
    target_symbol = original.symbol
    ordinary_notice = original.model_copy(
        update={
            "title": "关于公司经营风险提示的公告",
            "category_codes": ("RISK",),
            "official_verified": True,
        }
    )
    bundle = base.model_copy(
        update={
            "disclosures": (ordinary_notice, *base.disclosures[1:]),
            "events_by_symbol": {
                **base.events_by_symbol,
                target_symbol: (
                    ActiveEventRisk(
                        event_id=f"disclosure:{ordinary_notice.announcement_id}",
                        severity=EventSeverity.HIGH,
                        trusted_source=True,
                    ),
                ),
            },
        }
    )

    class Builder:
        def __init__(self) -> None:
            self.acquisition_events: list[dict[str, object]] = []

        def build(self, value_date, decision_at, *, required_symbols=()):
            del required_symbols
            assert value_date == trading_date
            assert decision_at == bundle.decision_at
            return bundle

    monkeypatch.setenv("CANONICAL_BUNDLE_MODE", "akshare")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v2.json",
        canonical_builder=Builder(),  # type: ignore[arg-type]
    )

    result = daily_research_flow(
        trading_date, ApplicationPipeline(backend, session_factory=factory)
    )

    with factory() as session:
        score = session.scalar(
            select(ScoreRow).where(
                ScoreRow.run_id == result["run_id"], ScoreRow.symbol == target_symbol
            )
        )
        assert score is not None
        assert score.event_risk_multiplier == 1


def _prepared_llm_backend(tmp_path, client: FakeStructuredLLMClient):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        app_env="development",
        llm_client=client,
    )
    pipeline = ApplicationPipeline(backend, session_factory=factory)
    run_id = pipeline.start_run(date(2026, 7, 14))
    pipeline.sync_reference_data(run_id)
    snapshots = pipeline.ingest_and_verify(run_id)
    universe_id = pipeline.build_universe(run_id, snapshots)
    feature_snapshot_id = pipeline.build_features(run_id, universe_id)
    return backend, factory, pipeline, run_id, feature_snapshot_id


class ReloadingPipeline:
    def __init__(self, factory, object_root, state_root, policy_path) -> None:
        self.factory = factory
        self.object_root = object_root
        self.state_root = state_root
        self.policy_path = policy_path

    def _application(self) -> ApplicationPipeline:
        backend = BuiltinDailyBackend(
            session_factory=self.factory,
            object_root=self.object_root,
            state_root=self.state_root,
            policy_path=self.policy_path,
            app_env="development",
        )
        return ApplicationPipeline(backend, session_factory=self.factory)

    def start_run(self, trading_date):
        return self._application().start_run(trading_date)

    def sync_reference_data(self, run_id):
        return self._application().sync_reference_data(run_id)

    def ingest_and_verify(self, run_id):
        return self._application().ingest_and_verify(run_id)

    def build_universe(self, run_id, snapshot_ids):
        return self._application().build_universe(run_id, snapshot_ids)

    def build_features(self, run_id, universe_id):
        return self._application().build_features(run_id, universe_id)

    def run_research_agents(self, run_id, feature_snapshot_id):
        return self._application().run_research_agents(run_id, feature_snapshot_id)

    def calculate_scores(self, run_id, agent_bundle_id):
        return self._application().calculate_scores(run_id, agent_bundle_id)

    def qlib_filter(self, run_id, score_snapshot_id):
        return self._application().qlib_filter(run_id, score_snapshot_id)

    def risk_state(self, run_id):
        return self._application().risk_state(run_id)

    def build_portfolio(self, run_id, candidate_snapshot_id):
        return self._application().build_portfolio(run_id, candidate_snapshot_id)

    def publish_report(self, run_id, portfolio_id, risk_state):
        return self._application().publish_report(run_id, portfolio_id, risk_state)

    def complete_run(self, run_id, report_id, status):
        return self._application().complete_run(run_id, report_id, status)


def test_custom_research_scores_only_requested_eligible_symbols(tmp_path) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        app_env="development",
    )
    pipeline = ApplicationPipeline(backend, session_factory=factory)
    run_id = pipeline.start_run(date(2026, 7, 14))
    targets = ["600000.SH", "600001.SH"]
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None
        run.manifest = {
            **run.manifest,
            "research_scope": "CUSTOM",
            "target_symbols": targets,
            "tracked_symbols": targets,
            "portfolio_requested": False,
        }
        session.commit()

    pipeline.sync_reference_data(run_id)
    snapshots = pipeline.ingest_and_verify(run_id)
    universe_id = pipeline.build_universe(run_id, snapshots)
    universe = backend._read_stage(run_id, "universe", UniverseResult)
    assert universe_id == backend._stage_digest(run_id, "universe")
    assert universe.included == tuple(targets)


def test_research_max_stock_price_filters_universe_before_feature_generation(tmp_path) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        app_env="development",
    )
    pipeline = ApplicationPipeline(backend, session_factory=factory)
    run_id = pipeline.start_run(date(2026, 7, 14))
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None
        run.manifest = {
            **run.manifest,
            "research_scope": "MARKET",
            "research_budget": {"max_stock_price": "10"},
        }
        session.commit()

    pipeline.sync_reference_data(run_id)
    snapshots = pipeline.ingest_and_verify(run_id)
    universe_id = pipeline.build_universe(run_id, snapshots)
    universe = backend._read_stage(run_id, "universe", UniverseResult)
    bundle = backend._read_stage(run_id, "bundle", CanonicalDailyBundle)
    latest_closes = {
        item.symbol: item.close
        for item in bundle.bars
        if item.trading_date == bundle.trading_date
    }

    assert universe.included
    assert all(latest_closes[symbol] <= 10 for symbol in universe.included)
    assert any(latest_closes[symbol] > 10 for symbol in latest_closes)
    pipeline.build_features(run_id, universe_id)
    features = backend._read_stage(run_id, "features", FeatureArtifact)
    assert {item.symbol for item in features.items} == set(universe.included)


def test_small_custom_research_succeeds_reports_all_symbols_and_freezes_advice_snapshot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trading_date = date(2026, 7, 14)
    base = make_demo_bundle(trading_date)
    targets = [base.securities[0].symbol, base.securities[1].symbol, base.securities[2].symbol]
    quality = dict(base.data_quality)
    quality[targets[2]] = {
        **quality[targets[2]],
        "fundamental_placeholder": True,
        "fundamental_reason_codes": ["INCOMPLETE_LATEST_FINANCIAL_PERIOD"],
    }
    bundle = base.model_copy(update={"data_quality": quality})

    class Builder:
        def __init__(self) -> None:
            self.acquisition_events: list[dict[str, object]] = []

        def build(self, value_date, decision_at, *, required_symbols=()):
            del required_symbols
            assert value_date == trading_date
            assert decision_at == bundle.decision_at
            return bundle

    monkeypatch.setenv("CANONICAL_BUNDLE_MODE", "akshare")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        canonical_builder=Builder(),  # type: ignore[arg-type]
    )
    pipeline = ApplicationPipeline(backend, session_factory=factory)
    run_id = pipeline.start_run(trading_date)
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None
        run.user_id = "user-small"
        run.manifest = {
            **run.manifest,
            "research_scope": "CUSTOM",
            "target_symbols": targets,
            "tracked_symbols": targets,
            "portfolio_requested": False,
        }
        session.commit()

    pipeline.sync_reference_data(run_id)
    snapshot_ids = pipeline.ingest_and_verify(run_id)
    universe_id = pipeline.build_universe(run_id, snapshot_ids)
    feature_id = pipeline.build_features(run_id, universe_id)
    agent_id = pipeline.run_research_agents(run_id, feature_id)
    score_id = pipeline.calculate_scores(run_id, agent_id)
    candidate_id = pipeline.qlib_filter(run_id, score_id)
    assert pipeline.risk_state(run_id) != "OBSERVE_ONLY"
    assert pipeline.portfolio_requested(run_id) is False
    report_id = pipeline.publish_report(run_id, None, "NORMAL")
    result = pipeline.complete_run(run_id, report_id, "SUCCEEDED")

    assert result["status"] == "SUCCEEDED"
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None
        assert run.manifest["portfolio_outcome"]["reason_code"] == ("INSUFFICIENT_DIVERSIFICATION")
        assert session.scalar(select(PortfolioRow).where(PortfolioRow.run_id == run_id)) is None
        assert len(session.scalars(select(ScoreRow).where(ScoreRow.run_id == run_id)).all()) == 3
        assert (
            len(session.scalars(select(CandidateRow).where(CandidateRow.run_id == run_id)).all())
            == 2
        )
        snapshot = session.scalar(
            select(SnapshotManifestRow).where(
                SnapshotManifestRow.run_id == run_id,
                SnapshotManifestRow.dataset == "backtest_bundle",
            )
        )
        assert snapshot is not None
        assert snapshot.details["snapshot_purpose"] == "SINGLE_SYMBOL_ADVICE"
        report = session.get(ReportRow, report_id)
        assert report is not None
        html = backend.object_store.get(report.object_uri).decode("utf-8")
        assert all(symbol in html for symbol in targets)
        assert "暂不买入" in html
        assert "T+1 交易时序" in html
        assert "最早可卖日" in html
        assert candidate_id == backend._stage_digest(run_id, "candidates")


def test_builtin_demo_runs_full_daily_flow_and_is_reproducible(tmp_path) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    object_root = tmp_path / "objects"
    state_root = tmp_path / "state"
    policy_path = "configs/first_release.v1.json"
    pipeline = ReloadingPipeline(factory, object_root, state_root, policy_path)
    trading_date = date(2026, 7, 14)

    first = daily_research_flow(trading_date, pipeline)
    second = daily_research_flow(trading_date, pipeline)
    assert first["status"] == second["status"] == "SUCCEEDED"

    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=object_root,
        state_root=state_root,
        policy_path=policy_path,
        app_env="development",
    )
    stable_stages = (
        "bundle",
        "universe",
        "features",
        "agents",
        "scores",
        "candidates",
        "portfolio",
    )
    first_state = backend.state_for_run(first["run_id"])
    second_state = backend.state_for_run(second["run_id"])
    assert {stage: first_state[stage]["sha256"] for stage in stable_stages} == {
        stage: second_state[stage]["sha256"] for stage in stable_stages
    }

    with factory() as session:
        first_run = session.get(JobRun, first["run_id"])
        assert first_run is not None and first_run.status == "SUCCEEDED"
        scores = session.scalars(select(ScoreRow).where(ScoreRow.run_id == first["run_id"])).all()
        assert len(scores) == 20
        candidate_rows = session.scalars(
            select(CandidateRow).where(CandidateRow.run_id == first["run_id"])
        ).all()
        assert len(candidate_rows) == 20
        assert all(
            (row.industry_name or "").startswith("示例行业") for row in candidate_rows
        )
        portfolio = session.scalar(
            select(PortfolioRow).where(PortfolioRow.run_id == first["run_id"])
        )
        assert portfolio is not None and len(portfolio.positions) == 15
        report = session.scalar(select(ReportRow).where(ReportRow.run_id == first["run_id"]))
        assert report is not None
        assert backend.object_store.get(report.object_uri).startswith(b"<!doctype html>")
        event_types = set(
            session.scalars(
                select(AuditEvent.event_type).where(AuditEvent.run_id == first["run_id"])
            ).all()
        )
        assert {"RUN_STARTED", "STAGE_COMPLETED", "RUN_COMPLETED"} <= event_types
        assert (
            len(session.scalars(select(AgentCall).where(AgentCall.run_id == first["run_id"])).all())
            == 60
        )


@pytest.mark.parametrize(
    ("complete_count", "expected_status", "expected_portfolio"),
    [(15, "SUCCEEDED", True), (14, "FUSED", False)],
)
def test_formal_portfolio_gate_is_per_symbol(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    complete_count: int,
    expected_status: str,
    expected_portfolio: bool,
) -> None:
    monkeypatch.setenv("CANONICAL_BUNDLE_MODE", "akshare")
    get_settings.cache_clear()
    trading_date = date(2026, 7, 14)
    base = make_demo_bundle(trading_date)
    quality = {}
    for index, (symbol, values) in enumerate(base.data_quality.items()):
        complete = index < complete_count
        quality[symbol] = {
            **values,
            "fundamental_placeholder": not complete,
            "sentiment_placeholder": not complete,
            "fundamental_reason_codes": [] if complete else ["INCOMPLETE_LATEST_FINANCIAL_PERIOD"],
            "sentiment_reason_codes": [] if complete else ["MISSING_OFFICIAL_DISCLOSURE"],
        }
    bundle = base.model_copy(update={"data_quality": quality})

    class Builder:
        def __init__(self) -> None:
            self.acquisition_events: list[dict[str, object]] = []

        def build(self, value_date, decision_at, *, required_symbols=()):
            del required_symbols
            assert value_date == trading_date
            assert decision_at == bundle.decision_at
            return bundle

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        app_env="development",
        canonical_builder=Builder(),  # type: ignore[arg-type]
    )
    result = daily_research_flow(
        trading_date,
        ApplicationPipeline(backend, session_factory=factory),
    )

    assert result["status"] == expected_status
    with factory() as session:
        run = session.get(JobRun, result["run_id"])
        assert run is not None
        gate = run.manifest["data_quality_gate"]
        assert gate["formal_eligible_count"] == complete_count
        assert len(gate["excluded_symbols"]) == 20 - complete_count
        candidates = session.scalars(
            select(CandidateRow).where(CandidateRow.run_id == result["run_id"])
        ).all()
        assert len(candidates) == complete_count
        portfolio = session.scalar(
            select(PortfolioRow).where(PortfolioRow.run_id == result["run_id"])
        )
        assert (portfolio is not None) is expected_portfolio
        if expected_status == "FUSED":
            assert run.manifest["risk_outcome"]["reason_code"] == ("INSUFFICIENT_COMPLETE_SYMBOLS")


def test_builtin_production_requires_canonical_bundle_and_accepts_json(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("ASHARE_CANONICAL_BUNDLE", raising=False)
    monkeypatch.setenv("CANONICAL_BUNDLE_MODE", "file")
    get_settings.cache_clear()
    backend = BuiltinDailyBackend(
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        app_env="production",
    )
    trading_date = date(2026, 7, 15)
    with pytest.raises(RuntimeError, match="ASHARE_CANONICAL_BUNDLE"):
        backend.run_manifest(trading_date, market_decision_time(trading_date))

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(make_demo_bundle(trading_date).model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("ASHARE_CANONICAL_BUNDLE", str(bundle_path))
    manifest = backend.run_manifest(trading_date, market_decision_time(trading_date))
    assert manifest["backend"] == "builtin-deterministic-v1"
    assert len(manifest["canonical_file_sha256"]) == 64


def test_historical_research_reuses_bundle_from_failed_source_run(tmp_path) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    backend = BuiltinDailyBackend(
        session_factory=factory,
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        app_env="development",
    )
    pipeline = ApplicationPipeline(backend, session_factory=factory)
    trading_date = date(2026, 7, 14)
    source_run_id = pipeline.start_run(trading_date)
    with factory() as session:
        source = session.get(JobRun, source_run_id)
        assert source is not None
        source.user_id = "research-owner"
        session.commit()

    pipeline.sync_reference_data(source_run_id)
    with factory() as session:
        source = session.get(JobRun, source_run_id)
        assert source is not None
        source.status = "FAILED"
        source.completed_at = datetime.now(UTC)
        session.commit()

    rerun_id = pipeline.start_run(trading_date)
    with factory() as session:
        rerun = session.get(JobRun, rerun_id)
        assert rerun is not None
        rerun.user_id = "research-owner"
        rerun.manifest = {**rerun.manifest, "source_bundle_run_id": source_run_id}
        session.commit()

    pipeline.sync_reference_data(rerun_id)

    with factory() as session:
        source = session.get(JobRun, source_run_id)
        rerun = session.get(JobRun, rerun_id)
        assert source is not None and rerun is not None
        assert rerun.manifest["acquired_bundle_sha256"] == source.manifest[
            "acquired_bundle_sha256"
        ]
        assert session.scalar(
            select(AuditEvent.event_type).where(
                AuditEvent.run_id == rerun_id,
                AuditEvent.event_type == "HISTORICAL_BUNDLE_REUSED",
            )
        ) == "HISTORICAL_BUNDLE_REUSED"
    assert backend._stage_digest(rerun_id, "bundle") == backend._stage_digest(
        source_run_id, "bundle"
    )


def test_llm_component_results_are_audited_with_transport_metadata(tmp_path) -> None:
    client = FakeStructuredLLMClient()
    _, factory, pipeline, run_id, feature_snapshot_id = _prepared_llm_backend(tmp_path, client)

    pipeline.run_research_agents(run_id, feature_snapshot_id)

    assert len(client.requests) == 60
    for request in client.requests:
        assert request["features"]
        assert all(
            value is None or isinstance(value, (str, bool, int, float))
            for value in request["features"].values()
        )
        assert len(request["evidence"]) == 1
    with factory() as session:
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)).all()
        assert len(calls) == 60
        assert {call.model_provider for call in calls} == {"fake-provider"}
        assert {call.model_name for call in calls} == {"fake-model"}
        assert {call.result["score"] for call in calls if call.component == "fundamental"} == {71.0}
        assert all(call.result["prompt_version"] == "builtin-llm-v3" for call in calls)


def test_llm_research_reuses_local_component_cache_and_shared_prefix(tmp_path) -> None:
    client = FakeStructuredLLMClient()
    backend, factory, pipeline, run_id, feature_snapshot_id = _prepared_llm_backend(
        tmp_path, client
    )
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            UserAccount(
                user_id="cache-research-user",
                username="cache-research-user",
                password_hash="fixture",
                role="USER",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        run = session.get(JobRun, run_id)
        assert run is not None
        run.user_id = "cache-research-user"
        session.commit()

    backend._settings = backend._settings.model_copy(update={"llm_agent_max_concurrency": 1})
    pipeline.run_research_agents(run_id, feature_snapshot_id)
    with factory() as session:
        assert session.query(AIResponseCacheRow).count() == 60
    pipeline.run_research_agents(run_id, feature_snapshot_id)

    assert len(client.requests) == 60
    assert len(client.messages) == 60
    assert len({messages[0]["content"] for messages in client.messages}) == 1
    assert "FROZEN BATCH CONTEXT" in client.messages[0][0]["content"]
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None
        assert run.manifest["agent_cache"]["layers"]["LOCAL"] == 60


def test_llm_unavailability_falls_back_to_deterministic_agents(tmp_path) -> None:
    client = UnavailableStructuredLLMClient()
    _, factory, pipeline, run_id, feature_snapshot_id = _prepared_llm_backend(tmp_path, client)  # type: ignore[arg-type]

    pipeline.run_research_agents(run_id, feature_snapshot_id)

    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None
        assert run.manifest["agent_backend_effective"] == "builtin-deterministic-fallback"
        assert run.manifest["agent_backend_degradation"]["status_code"] == 403
        assert session.scalar(
            select(AuditEvent.event_type).where(
                AuditEvent.run_id == run_id,
                AuditEvent.event_type == "LLM_AGENT_DEGRADED",
            )
        ) == "LLM_AGENT_DEGRADED"
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)).all()
        assert len(calls) == 60
        assert {call.model_provider for call in calls} == {"builtin"}


def test_llm_component_requests_use_configured_bounded_concurrency(tmp_path) -> None:
    client = ConcurrentFakeStructuredLLMClient()
    backend, _, pipeline, run_id, feature_snapshot_id = _prepared_llm_backend(tmp_path, client)
    backend._settings = backend._settings.model_copy(update={"llm_agent_max_concurrency": 2})

    pipeline.run_research_agents(run_id, feature_snapshot_id)

    assert client.maximum_active == 2


def test_llm_component_rejects_future_evidence(tmp_path) -> None:
    client = FakeStructuredLLMClient(future_evidence=True)
    _, factory, pipeline, run_id, feature_snapshot_id = _prepared_llm_backend(tmp_path, client)

    with pytest.raises(ValueError, match="future information"):
        pipeline.run_research_agents(run_id, feature_snapshot_id)
    with factory() as session:
        assert session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)).all() == []
