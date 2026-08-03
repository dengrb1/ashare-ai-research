from __future__ import annotations

import asyncio
import json
import math
import os
import threading
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ashare_ai.agents.protocols import AgentRequest, StructuredLLMClient
from ashare_ai.agents.validation import run_component_agent
from ashare_ai.backtest.engine import BacktestSignal
from ashare_ai.core.contracts import (
    AgentComponentResult,
    Candidate,
    CompositeScore,
    DataQualityInputs,
    EvidenceRef,
    FrozenModel,
    PointInTimeRecord,
)
from ashare_ai.core.hashing import canonical_json, sha256_bytes, stable_hash
from ashare_ai.core.system_settings import get_effective_settings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.features.fundamental import FundamentalFeatures, extract_fundamental_features
from ashare_ai.features.sentiment import SentimentFeatures, extract_sentiment_features
from ashare_ai.features.technical import TechnicalFeatures, extract_technical_features
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.orchestration.akshare_bundle import (
    AKShareCanonicalBundleBuilder,
    AKShareCanonicalProvider,
    CanonicalMarketProvider,
    FallbackCanonicalProvider,
    MarketDataAcquisitionError,
    TushareCanonicalProvider,
)
from ashare_ai.orchestration.backtest_snapshot import create_backtest_snapshot
from ashare_ai.orchestration.builtin_backtest import BacktestBundle, read_backtest_bundle
from ashare_ai.orchestration.bundle import (
    CanonicalDailyBundle,
    evidence_payload,
    make_demo_bundle,
)
from ashare_ai.portfolio.builder import CandidateQuote, PortfolioBuilder, PortfolioConfig
from ashare_ai.portfolio.events import (
    EventRiskPolicy,
    EventSeverity,
    aggregate_event_risk,
    classify_event_risks,
)
from ashare_ai.portfolio.risk import (
    DrawdownConfig,
    DrawdownControlState,
    PortfolioRiskState,
    transition_drawdown_state,
)
from ashare_ai.reports.chinese_summary import component_summary, symbol_summary
from ashare_ai.reports.daily import DailyReportService
from ashare_ai.scoring.dividends import calculate_dividend_bonus
from ashare_ai.scoring.formula import FORMULA_VERSION, FORMULA_VERSION_V2, build_composite_score
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.lake import ImmutableLake
from ashare_ai.storage.models import (
    AgentCall,
    CandidateRow,
    JobRun,
    PortfolioRow,
    ReportRow,
    ScoreRow,
    SecurityMaster,
    SnapshotManifestRow,
)
from ashare_ai.storage.object_service import StoredObjectService
from ashare_ai.storage.objects import LocalObjectStore
from ashare_ai.storage.repositories import SnapshotRepository
from ashare_ai.trading.default_rules import RULESET_VERSION
from ashare_ai.universe.builder import UniverseConfig, UniverseResult, build_dynamic_universe

M = TypeVar("M", bound=BaseModel)
T = TypeVar("T")

_COMPONENT_SYSTEM_INSTRUCTIONS: dict[str, str] = {
    "fundamental": (
        "You are the fundamental analyst. Return only an evidence-grounded fundamental "
        "subscore and concise factors from the supplied request. Cite only supplied evidence. "
        "Score must be 0-100 and confidence must be 0-1. You cannot set a final score, "
        "portfolio weight, or target price. All positive_factors, negative_factors, and "
        "risk_flags must be concise simplified Chinese for ordinary investors; "
        "do not promise returns."
    ),
    "technical": (
        "You are the technical analyst. Return only an evidence-grounded technical subscore "
        "and concise factors from the supplied request. Cite only supplied evidence. You "
        "must return score 0-100 and confidence 0-1, and cannot set a final score, "
        "portfolio weight, or target price. All positive_factors, negative_factors, and "
        "risk_flags must be concise simplified Chinese for ordinary investors; "
        "do not promise returns."
    ),
    "sentiment": (
        "You are the sentiment analyst. Return only an evidence-grounded sentiment subscore "
        "and concise factors from the supplied request. Cite only supplied evidence. You "
        "must return score 0-100 and confidence 0-1, and cannot set a final score, "
        "portfolio weight, or target price. All positive_factors, negative_factors, and "
        "risk_flags must be concise simplified Chinese for ordinary investors; "
        "do not promise returns."
    ),
}


class ScoringPolicy(FrozenModel):
    formula_version: str
    fundamental_weight: Decimal
    technical_weight: Decimal
    sentiment_weight: Decimal
    quality_confidence_weight: Decimal
    dividend_yield_multiplier: Decimal = Decimal("200")
    dividend_yield_bonus_cap: Decimal = Decimal("8")
    dividend_continuity_bonus_cap: Decimal = Decimal("2")
    dividend_total_bonus_cap: Decimal = Decimal("10")
    news_window_days: int = Field(default=30, ge=1)
    event_classifier_version: str = "dividend-news-events-v2"
    event_risk_multipliers: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "LOW": Decimal("1"),
            "MEDIUM": Decimal("0.8"),
            "HIGH": Decimal("0.5"),
            "CRITICAL": Decimal("0"),
        }
    )


class UniversePolicy(FrozenModel):
    minimum_listing_days: int = Field(ge=0)
    liquidity_window: int = Field(gt=0)
    minimum_average_amount: float = Field(ge=0)
    abnormal_return_window: int = Field(gt=0)


class PortfolioPolicy(FrozenModel):
    target_count: int = Field(gt=0)
    maximum_single_weight: Decimal = Field(gt=0, le=1)
    maximum_industry_weight: Decimal = Field(gt=0, le=1)
    maximum_one_way_turnover: Decimal = Field(ge=0, le=2)
    cash_buffer: Decimal = Field(ge=0, lt=1)
    style_exposure_limits: dict[str, Decimal]


class ExecutionPolicy(FrozenModel):
    participation_rate: Decimal = Field(gt=0, le=1)
    impact_coefficient: Decimal = Field(ge=0)
    maximum_slippage_bps: Decimal = Field(ge=0)


class RiskPolicy(FrozenModel):
    derisk_drawdown: Decimal = Field(gt=0, lt=1)
    observe_only_drawdown: Decimal = Field(gt=0, lt=1)
    minimum_observation_sessions: int = Field(ge=0)
    recovery_drawdown: Decimal = Field(ge=0, lt=1)
    manual_recovery_required: bool


class BacktestPolicy(FrozenModel):
    required_benchmarks: tuple[str, str, str]
    capacity_max_participation: Decimal = Field(gt=0, le=1)
    capacity_min_fill_rate: Decimal = Field(gt=0, le=1)
    capacity_max_impact_bps: Decimal = Field(ge=0)


class TradePlanPolicy(FrozenModel):
    objective: str = "RISK_ADJUSTED_RETURN"
    history_sessions: int = Field(default=240, ge=120, le=500)
    training_sessions: int = Field(default=160, ge=1)
    validation_sessions: int = Field(default=80, ge=1)
    entry_step_sessions: int = Field(default=10, ge=1)
    minimum_completed_trades: int = Field(default=5, ge=1)
    maximum_drawdown: Decimal = Field(default=Decimal("0.12"), gt=0, lt=1)
    entry_discounts: tuple[Decimal, ...] = (
        Decimal("0"),
        Decimal("0.01"),
        Decimal("0.02"),
    )
    take_profits: tuple[Decimal, ...] = (
        Decimal("0.08"),
        Decimal("0.12"),
        Decimal("0.16"),
    )
    stop_losses: tuple[Decimal, ...] = (
        Decimal("0.05"),
        Decimal("0.08"),
        Decimal("0.10"),
    )
    trailing_stops: tuple[Decimal, ...] = (Decimal("0.05"), Decimal("0.08"))
    maximum_holding_sessions: tuple[int, ...] = (10, 20, 40, 60)
    entry_valid_sessions: int = Field(default=3, ge=1)
    score_exit_threshold: Decimal = Field(default=Decimal("60"), ge=0, le=100)


class ChatPolicy(FrozenModel):
    context_cache_seconds: int = Field(default=120, ge=15, le=3600)
    answer_cache_hours: int = Field(default=24, ge=1, le=168)
    max_daily_kline_concurrency: int = Field(default=4, ge=1, le=16)
    news_window_days: int = Field(default=30, ge=1, le=365)
    max_news_results: int = Field(default=5, ge=1, le=5)


class MonitoringPolicy(FrozenModel):
    stop_loss_enabled_by_default: bool = True
    stop_loss_atr_sessions: int = Field(default=20, ge=5, le=120)
    stop_loss_atr_multiple: Decimal = Field(default=Decimal("2"), gt=0, le=10)
    stop_loss_min_pct: Decimal = Field(default=Decimal("0.05"), gt=0, lt=1)
    stop_loss_max_pct: Decimal = Field(default=Decimal("0.10"), gt=0, lt=1)
    stop_loss_fallback_pct: Decimal = Field(default=Decimal("0.08"), gt=0, lt=1)
    stop_loss_cooldown_minutes: int = Field(default=30, ge=1, le=24 * 60)
    buy_monitor_valid_sessions: int = Field(default=1, ge=1, le=5)
    buy_entry_band_pct: Decimal = Field(default=Decimal("0.02"), gt=0, le=Decimal("0.10"))


class NotificationPolicy(FrozenModel):
    read_retention_days: int = Field(default=90, ge=1, le=3650)
    unread_retention_days: int = Field(default=180, ge=1, le=3650)
    page_limit: int = Field(default=50, ge=1, le=200)


class FirstReleasePolicy(FrozenModel):
    version: str
    scoring: ScoringPolicy
    universe: UniversePolicy
    portfolio: PortfolioPolicy
    execution: ExecutionPolicy
    risk: RiskPolicy
    backtest: BacktestPolicy
    trade_plan: TradePlanPolicy = TradePlanPolicy()
    chat: ChatPolicy = ChatPolicy()
    monitoring: MonitoringPolicy = MonitoringPolicy()
    notifications: NotificationPolicy = NotificationPolicy()

    @model_validator(mode="after")
    def enforce_release_contract(self) -> FirstReleasePolicy:
        weights = (
            self.scoring.fundamental_weight,
            self.scoring.technical_weight,
            self.scoring.sentiment_weight,
            self.scoring.quality_confidence_weight,
        )
        if weights != (Decimal("0.35"), Decimal("0.35"), Decimal("0.20"), Decimal("0.10")):
            raise ValueError("first release scoring weights must be 35/35/20/10")
        if self.scoring.formula_version not in {FORMULA_VERSION, FORMULA_VERSION_V2}:
            raise ValueError("first release formula version does not match scoring engine")
        if (
            self.trade_plan.training_sessions + self.trade_plan.validation_sessions
            > self.trade_plan.history_sessions
        ):
            raise ValueError("trade-plan train/validation windows exceed history window")
        if (
            self.portfolio.target_count != 15
            or self.portfolio.maximum_single_weight != Decimal("0.08")
            or self.portfolio.maximum_industry_weight != Decimal("0.25")
            or self.portfolio.maximum_one_way_turnover != Decimal("0.20")
        ):
            raise ValueError("first release portfolio policy must enforce 15/8%/25%/20%")
        if set(self.backtest.required_benchmarks) != {
            "CSI300",
            "CSI500",
            "EQUAL_WEIGHT_UNIVERSE",
        }:
            raise ValueError("first release requires CSI300, CSI500 and equal-weight universe")
        return self


class SymbolFeatureSet(FrozenModel):
    symbol: str
    fundamental: FundamentalFeatures
    technical: TechnicalFeatures
    sentiment: SentimentFeatures


class FeatureArtifact(FrozenModel):
    trading_date: date
    decision_at: AwareDatetime
    items: tuple[SymbolFeatureSet, ...]


class SymbolAgentSet(FrozenModel):
    symbol: str
    results: tuple[AgentComponentResult, AgentComponentResult, AgentComponentResult]


class AgentArtifact(FrozenModel):
    trading_date: date
    decision_at: AwareDatetime
    items: tuple[SymbolAgentSet, ...]


class ScoreArtifact(FrozenModel):
    scores: tuple[CompositeScore, ...]


class CandidateArtifact(FrozenModel):
    candidates: tuple[Candidate, ...]


class RiskArtifact(FrozenModel):
    control: DrawdownControlState
    formal_eligible_symbols: tuple[str, ...] = ()
    excluded_symbols: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    reason_code: str | None = None
    reason_message: str | None = None


class BuiltinDailyBackend:
    version = "builtin-deterministic-v1"

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        object_root: str | Path | None = None,
        state_root: str | Path | None = None,
        policy_path: str | Path | None = None,
        app_env: str | None = None,
        llm_client: StructuredLLMClient | None = None,
        canonical_builder: AKShareCanonicalBundleBuilder | None = None,
        allow_demo_data: bool | None = None,
    ) -> None:
        settings = get_effective_settings()
        self._settings = settings
        self._injected_llm_client = llm_client
        self._configured_llm_client: StructuredLLMClient | None = None
        self._canonical_builder = canonical_builder
        self._allow_demo_data = (
            settings.allow_demo_data if allow_demo_data is None else allow_demo_data
        )
        self.session_factory = session_factory
        self.app_env = (app_env or os.environ.get("APP_ENV") or settings.app_env).casefold()
        default_object_root = Path(settings.lake_root).parent / "objects"
        self.object_root = Path(
            object_root or os.environ.get("ASHARE_BUILTIN_OBJECT_ROOT") or default_object_root
        )
        self.state_root = Path(
            state_root
            or os.environ.get("ASHARE_BUILTIN_STATE_ROOT")
            or self.object_root / "builtin-state"
        )
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.object_store = LocalObjectStore(self.object_root)
        self.policy_path = Path(policy_path or settings.policy_config_path)
        self.policy = FirstReleasePolicy.model_validate_json(
            self.policy_path.read_text(encoding="utf-8")
        )

    def run_manifest(self, trading_date: date, decision_at: datetime) -> dict[str, Any]:
        mode = (
            "file"
            if os.environ.get("ASHARE_CANONICAL_BUNDLE")
            else self._settings.canonical_bundle_mode
        )
        if mode == "demo" and not self._allow_demo_data:
            raise RuntimeError("demo canonical data requires ALLOW_DEMO_DATA=true")
        if mode == "file" and not os.environ.get("ASHARE_CANONICAL_BUNDLE"):
            raise RuntimeError("canonical_bundle_mode=file requires ASHARE_CANONICAL_BUNDLE")
        manifest: dict[str, Any] = {
            "backend": self.version,
            "canonical_source_mode": mode,
            "canonical_provider": (
                "akshare+tushare"
                if mode == "akshare" and self._settings.tushare_token
                else "akshare"
                if mode == "akshare"
                else mode
            ),
            "canonical_provider_version": "akshare-canonical-v3-raw-news-dividend",
            "canonical_bundle_size": self._settings.akshare_bundle_size,
            "canonical_history_sessions": self._settings.akshare_history_sessions,
            "policy_version": self.policy.version,
            "policy_sha256": sha256_bytes(self.policy_path.read_bytes()),
            "formula_version": self.policy.scoring.formula_version,
            "random_seed": 42,
            "tushare_fallback_configured": bool(self._settings.tushare_token),
        }
        configured = os.environ.get("ASHARE_CANONICAL_BUNDLE")
        if configured:
            if configured.strip().startswith("{"):
                manifest["canonical_inline_sha256"] = sha256_bytes(configured.encode("utf-8"))
            else:
                path = Path(configured)
                manifest["canonical_file_sha256"] = sha256_bytes(path.read_bytes())
                manifest["canonical_file_path"] = str(path)
        return manifest

    def sync_reference_data(self, run_id: str) -> None:
        trading_date, decision_at, manifest = self._run_context(run_id)
        state = self._load_state(run_id)
        if "bundle" in state:
            bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        else:
            configured = os.environ.get("ASHARE_CANONICAL_BUNDLE")
            if configured:
                actual = (
                    sha256_bytes(configured.encode("utf-8"))
                    if configured.strip().startswith("{")
                    else sha256_bytes(Path(configured).read_bytes())
                )
                expected = manifest.get(
                    "canonical_inline_sha256"
                    if configured.strip().startswith("{")
                    else "canonical_file_sha256"
                )
                if actual != expected:
                    raise RuntimeError("configured canonical bundle changed after submission")
            bundle = self._load_source_bundle(
                trading_date,
                decision_at,
                run_id=run_id,
                required_symbols=tuple(manifest.get("tracked_symbols", ())),
            )
            digest = self._write_stage(run_id, "bundle", bundle)
            with self.session_factory() as session:
                run = session.get(JobRun, run_id)
                if run is None:
                    raise KeyError(run_id)
                updated = dict(run.manifest)
                updated.update(
                    acquired_bundle_sha256=digest,
                    bundle_schema_version=bundle.schema_version,
                    data_quality=bundle.data_quality,
                )
                run.manifest = updated
                source_run_id = manifest.get("source_bundle_run_id")
                if isinstance(source_run_id, str) and source_run_id:
                    AuditLogger(session).record(
                        run_id,
                        "HISTORICAL_BUNDLE_REUSED",
                        "Research reused an immutable bundle from the latest completed session",
                        details={
                            "source_run_id": source_run_id,
                            "bundle_sha256": digest,
                        },
                    )
                session.commit()
        self._write_stage(
            run_id,
            "reference",
            {
                "securities": len(bundle.securities),
                "statuses": len(bundle.statuses),
                "industries": len(bundle.industries),
            },
        )

    def ingest_and_verify(self, run_id: str) -> list[str]:
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        digest = self._stage_digest(run_id, "bundle")
        with self.session_factory() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(run_id)
            snapshot = create_backtest_snapshot(
                session=session,
                run=run,
                bundle=bundle,
                lake_root=self._settings.lake_root,
                policy=self.policy,
                dataset="research_input_bundle",
            )
            canonical_snapshot_ids = self._persist_canonical_evidence_snapshots(
                session, run, bundle
            )
            self._project_security_master(session, bundle)
            updated = dict(run.manifest)
            updated["canonical_snapshot_ids"] = canonical_snapshot_ids
            run.manifest = updated
            session.commit()
        self._write_stage(
            run_id,
            "ingestion",
            {
                "bundle_sha256": digest,
                "bars": len(bundle.bars),
                "financial_facts": len(bundle.financial_facts),
                "documents": len(bundle.disclosures) + len(bundle.news),
                "dividends": len(bundle.dividends),
                "canonical_snapshot_ids": canonical_snapshot_ids,
                "backtest_snapshot_id": snapshot.snapshot_id,
            },
        )
        return [snapshot.snapshot_id]

    @staticmethod
    def _project_security_master(session: Session, bundle: CanonicalDailyBundle) -> None:
        """Expose a read-only projection of committed canonical security evidence.

        Chat and paper-risk APIs need a small indexed lookup surface, but must not
        treat a provider response as authoritative on its own.  This projection is
        written only after the immutable input and canonical evidence manifests have
        been committed in the same ingestion transaction.  Existing facts are never
        overwritten: a correction with the same source/effective date remains
        available from its frozen manifest while the established master identity is
        retained for point-in-time API resolution.
        """

        statuses = {
            item.symbol: item
            for item in bundle.statuses
            if item.available_at <= bundle.decision_at
            and item.effective_from <= bundle.trading_date
            and (item.effective_to is None or item.effective_to >= bundle.trading_date)
        }
        industries = {
            item.symbol: item
            for item in bundle.industries
            if item.available_at <= bundle.decision_at
            and item.effective_from <= bundle.trading_date
            and (item.effective_to is None or item.effective_to >= bundle.trading_date)
        }
        for record in (*bundle.securities, *bundle.security_directory):
            existing = session.scalar(
                select(SecurityMaster).where(
                    SecurityMaster.symbol == record.symbol,
                    SecurityMaster.effective_from == record.effective_from,
                    SecurityMaster.source == record.source,
                )
            )
            if existing is not None:
                continue
            status = statuses.get(record.symbol)
            industry = industries.get(record.symbol)
            session.add(
                SecurityMaster(
                    symbol=record.symbol,
                    trading_date=record.trading_date,
                    exchange=record.exchange.value,
                    board=record.board.value,
                    short_name=record.short_name,
                    list_date=record.list_date,
                    delist_date=record.delist_date,
                    effective_from=record.effective_from,
                    effective_to=record.effective_to,
                    is_st=status.is_st if status is not None else False,
                    is_suspended=status.is_suspended if status is not None else False,
                    industry_code=industry.industry_code if industry is not None else None,
                    source=record.source,
                    source_record_id=record.source_record_id,
                    available_at=record.available_at,
                    fetched_at=record.fetched_at,
                    payload_sha256=record.payload_sha256,
                    schema_version=record.schema_version,
                    adapter_version=record.adapter_version,
                    ingestion_run_id=str(record.ingestion_run_id),
                    availability_basis=record.availability_basis.value,
                )
            )

    def _persist_canonical_evidence_snapshots(
        self,
        session: Session,
        run: JobRun,
        bundle: CanonicalDailyBundle,
    ) -> list[str]:
        lake = ImmutableLake(self._settings.lake_root)
        repository = SnapshotRepository(session)
        datasets: tuple[tuple[str, str, list[dict[str, Any]]], ...] = (
            (
                "canonical_security_master",
                "canonical-bundle",
                [
                    item.model_dump(mode="json")
                    for item in (*bundle.securities, *bundle.security_directory)
                ],
            ),
            (
                "canonical_news",
                "multi-free-news",
                [item.model_dump(mode="json") for item in bundle.news],
            ),
            (
                "canonical_cash_dividends",
                "cninfo-with-free-cross-check",
                [item.model_dump(mode="json") for item in bundle.dividends],
            ),
            (
                "canonical_trading_calendar",
                bundle.calendar_source,
                [
                    {
                        "trading_date": value.isoformat(),
                        "available_at": bundle.decision_at.isoformat(),
                        "source": bundle.calendar_source,
                        "calendar_version": bundle.calendar_version,
                    }
                    for value in bundle.trading_calendar
                ],
            ),
        )
        snapshot_ids: list[str] = []
        for dataset, source, rows in datasets:
            snapshot_rows = [
                {
                    **item,
                    "_manifest_run_id": run.run_id,
                    "_manifest_trading_date": bundle.trading_date.isoformat(),
                }
                for item in rows
            ] or [
                {
                    "_manifest_only": True,
                    "_manifest_run_id": run.run_id,
                    "_manifest_trading_date": bundle.trading_date.isoformat(),
                }
            ]
            manifest = lake.write_snapshot(
                dataset=dataset,
                source=source,
                schema_version="canonical-pit-v1",
                adapter_version="builtin-canonical-evidence-v1",
                fetched_at=bundle.decision_at,
                rows=snapshot_rows,
                metadata={
                    "run_id": run.run_id,
                    "trading_date": bundle.trading_date.isoformat(),
                    "decision_at": bundle.decision_at.isoformat(),
                    "source_record_hashes": sorted(
                        str(item.get("payload_sha256", item.get("trading_date", "")))
                        for item in rows
                    ),
                },
            )
            row = repository.add(manifest, run_id=run.run_id)
            repository.commit(row.snapshot_id)
            snapshot_ids.append(row.snapshot_id)
        return snapshot_ids

    def build_universe(self, run_id: str, snapshot_ids: list[str]) -> str:
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        if len(snapshot_ids) != 1:
            raise ValueError("universe requires exactly one frozen backtest snapshot")
        with self.session_factory() as session:
            snapshot = session.get(SnapshotManifestRow, snapshot_ids[0])
            if (
                snapshot is None
                or snapshot.run_id != run_id
                or snapshot.status != "COMMITTED"
                or snapshot.details.get("bundle_payload_sha256") is None
            ):
                raise ValueError("universe input does not match the committed run snapshot")
        result = build_dynamic_universe(
            bundle.securities,
            bundle.statuses,
            bundle.bars,
            trading_date=bundle.trading_date,
            decision_at=bundle.decision_at,
            config=UniverseConfig(
                version=f"{self.policy.version}-universe",
                min_listing_days=self.policy.universe.minimum_listing_days,
                liquidity_window=self.policy.universe.liquidity_window,
                min_average_amount=self.policy.universe.minimum_average_amount,
                min_history_days=self.policy.universe.liquidity_window,
                abnormal_return_window=self.policy.universe.abnormal_return_window,
            ),
        )
        _, _, manifest = self._run_context(run_id)
        research_scope = str(manifest.get("research_scope", "MARKET")).upper()
        target_symbols = tuple(str(item) for item in manifest.get("target_symbols", ()))
        if research_scope != "MARKET":
            target_set = set(target_symbols)
            included = tuple(symbol for symbol in result.included if symbol in target_set)
            if not included:
                rejected = {
                    item.symbol: [reason.value for reason in item.reasons]
                    for item in result.decisions
                    if item.symbol in target_set and not item.eligible
                }
                raise ValueError(
                    f"指定股票均未通过研究股票池校验: {rejected or list(target_symbols)}"
                )
            result = result.model_copy(
                update={
                    "included": included,
                    "input_hash": stable_hash(
                        {
                            "base_universe_hash": result.input_hash,
                            "research_scope": research_scope,
                            "target_symbols": target_symbols,
                        }
                    ),
                }
            )
        elif len(result.included) < self.policy.portfolio.target_count:
            raise ValueError("canonical bundle cannot produce the required 15-stock portfolio")
        return self._write_stage(run_id, "universe", result)

    def build_features(self, run_id: str, universe_id: str) -> str:
        if universe_id != self._stage_digest(run_id, "universe"):
            raise ValueError("feature stage received an unknown universe artifact")
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        universe = self._read_stage(run_id, "universe", UniverseResult)
        items = tuple(
            SymbolFeatureSet(
                symbol=symbol,
                fundamental=extract_fundamental_features(
                    bundle.financial_facts,
                    decision_at=bundle.decision_at,
                    trading_date=bundle.trading_date,
                    symbol=symbol,
                ),
                technical=extract_technical_features(
                    bundle.bars,
                    decision_at=bundle.decision_at,
                    trading_date=bundle.trading_date,
                    symbol=symbol,
                ),
                sentiment=extract_sentiment_features(
                    bundle.disclosures,
                    bundle.news,
                    symbol=symbol,
                    decision_at=bundle.decision_at,
                    trading_date=bundle.trading_date,
                ),
            )
            for symbol in universe.included
        )
        return self._write_stage(
            run_id,
            "features",
            FeatureArtifact(
                trading_date=bundle.trading_date,
                decision_at=bundle.decision_at,
                items=items,
            ),
        )

    def run_research_agents(self, run_id: str, feature_snapshot_id: str) -> str:
        if feature_snapshot_id != self._stage_digest(run_id, "features"):
            raise ValueError("agent stage received an unknown feature artifact")
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        features = self._read_stage(run_id, "features", FeatureArtifact)
        llm_client = self._resolve_llm_client(run_id)
        llm_results: dict[tuple[str, str], AgentComponentResult] = {}
        if llm_client is not None:
            try:
                llm_results = self._run_llm_components(
                    llm_client, bundle.decision_at, features.items, bundle
                )
            except Exception as exc:
                if not _is_degradable_llm_error(exc):
                    raise
                # Numeric scoring and the PIT evidence bundle remain valid when
                # the optional model gateway is unavailable.  Complete the run
                # with the same deterministic Agent mapping used when no model
                # configuration is enabled, and leave an auditable warning.
                llm_client = None
                self._record_llm_degradation(run_id, exc)
        items: list[SymbolAgentSet] = []
        for feature_item in features.items:
            evidence = self._evidence_for_symbol(bundle, feature_item.symbol)
            quality = bundle.data_quality[feature_item.symbol]
            if llm_client is None:
                results = (
                    self._agent_result(
                        "fundamental",
                        50.0
                        if quality.get("fundamental_placeholder")
                        else self._fundamental_score(feature_item.fundamental),
                        0.2
                        if quality.get("fundamental_placeholder")
                        else feature_item.fundamental.completeness,
                        evidence[0],
                        feature_item.fundamental,
                    ),
                    self._agent_result(
                        "technical",
                        self._technical_score(feature_item.technical),
                        feature_item.technical.completeness,
                        evidence[1],
                        feature_item.technical,
                    ),
                    self._agent_result(
                        "sentiment",
                        50.0
                        if quality.get("sentiment_placeholder")
                        else self._sentiment_score(feature_item.sentiment),
                        0.2
                        if quality.get("sentiment_placeholder")
                        else feature_item.sentiment.completeness,
                        evidence[2],
                        feature_item.sentiment,
                    ),
                )
            else:
                fundamental_result = (
                    self._agent_result(
                        "fundamental",
                        50.0,
                        0.2,
                        evidence[0],
                        feature_item.fundamental,
                    )
                    if quality.get("fundamental_placeholder")
                    else self._run_llm_component(
                        "fundamental",
                        feature_item.symbol,
                        llm_results,
                    )
                )
                sentiment_result = (
                    self._agent_result(
                        "sentiment",
                        50.0,
                        0.2,
                        evidence[2],
                        feature_item.sentiment,
                    )
                    if quality.get("sentiment_placeholder")
                    else self._run_llm_component(
                        "sentiment",
                        feature_item.symbol,
                        llm_results,
                    )
                )
                results = (
                    fundamental_result,
                    self._run_llm_component(
                        "technical",
                        feature_item.symbol,
                        llm_results,
                    ),
                    sentiment_result,
                )
            items.append(SymbolAgentSet(symbol=feature_item.symbol, results=results))
        artifact = AgentArtifact(
            trading_date=bundle.trading_date,
            decision_at=bundle.decision_at,
            items=tuple(items),
        )
        uri, digest = self.object_store.put(
            canonical_json(artifact), content_type="application/json"
        )
        del uri
        with self.session_factory() as session:
            object_service = StoredObjectService(session, self.object_store)
            audit = AuditLogger(session)
            for agent_item in artifact.items:
                for result in agent_item.results:
                    if session.scalar(
                        select(AgentCall.call_id).where(
                            AgentCall.run_id == run_id,
                            AgentCall.symbol == agent_item.symbol,
                            AgentCall.component == result.component,
                        )
                    ):
                        continue
                    for evidence_ref in result.evidence:
                        payload = evidence_payload(
                            evidence_ref.source, evidence_ref.source_record_id
                        )
                        if sha256_bytes(payload) == evidence_ref.payload_sha256:
                            object_service.put(
                                payload,
                                content_type="application/json",
                                source=evidence_ref.source,
                                source_record_id=evidence_ref.source_record_id,
                                fetched_at=evidence_ref.available_at,
                                available_at=evidence_ref.available_at,
                            )
                    audit.record_agent_result(
                        run_id=run_id,
                        symbol=agent_item.symbol,
                        request_sha256=stable_hash(
                            {"features": features, "component": result.component}
                        ),
                        result=result,
                        created_at=bundle.decision_at,
                    )
            session.commit()
        self._index_stage(run_id, "agents", uri=self._uri_for_digest(digest), digest=digest)
        return digest

    def calculate_scores(self, run_id: str, agent_bundle_id: str) -> str:
        if agent_bundle_id != self._stage_digest(run_id, "agents"):
            raise ValueError("score stage received an unknown agent artifact")
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        agents = self._read_stage(run_id, "agents", AgentArtifact)
        feature_digest = self._stage_digest(run_id, "features")
        feature_uuid = uuid5(NAMESPACE_URL, feature_digest)
        closes = {
            item.symbol: item.close
            for item in bundle.bars
            if item.trading_date == bundle.trading_date
        }
        event_policy = EventRiskPolicy(
            version=self.policy.scoring.event_classifier_version,
            multipliers={
                severity: float(self.policy.scoring.event_risk_multipliers[severity.value])
                for severity in EventSeverity
            },
            blocked_severities=frozenset({EventSeverity.CRITICAL}),
        )
        scores = tuple(
            build_composite_score(
                symbol=item.symbol,
                trading_date=bundle.trading_date,
                decision_at=bundle.decision_at,
                component_results=item.results,
                quality_inputs=DataQualityInputs(
                    completeness=float(
                        bundle.data_quality[item.symbol].get(
                            "completeness", sum(result.confidence for result in item.results) / 3
                        )
                    ),
                    freshness=1,
                    official_source_ratio=float(
                        bundle.data_quality[item.symbol].get("official_source_ratio", 1)
                    ),
                    cross_source_consistency=1,
                    schema_validity=1,
                    evidence_coverage=float(
                        bundle.data_quality[item.symbol].get("evidence_coverage", 1)
                    ),
                    mean_agent_confidence=sum(result.confidence for result in item.results) / 3,
                ),
                feature_snapshot_id=feature_uuid,
                formula_version=self.policy.scoring.formula_version,
                dividend_bonus=(
                    calculate_dividend_bonus(
                        bundle.dividends,
                        symbol=item.symbol,
                        decision_at=bundle.decision_at,
                        frozen_close=closes[item.symbol],
                        yield_multiplier=self.policy.scoring.dividend_yield_multiplier,
                        yield_bonus_cap=float(self.policy.scoring.dividend_yield_bonus_cap),
                        continuity_bonus_cap=float(
                            self.policy.scoring.dividend_continuity_bonus_cap
                        ),
                        total_bonus_cap=float(self.policy.scoring.dividend_total_bonus_cap),
                    ).total_bonus
                    if self.policy.scoring.formula_version == FORMULA_VERSION_V2
                    else 0.0
                ),
                event_risk_multiplier=aggregate_event_risk(
                    (
                        classify_event_risks(
                            bundle.disclosures,
                            bundle.news,
                            symbol=item.symbol,
                            decision_at=bundle.decision_at,
                            window_days=self.policy.scoring.news_window_days,
                        )
                        if self.policy.scoring.formula_version == FORMULA_VERSION_V2
                        else bundle.events_by_symbol.get(item.symbol, ())
                    ),
                    event_policy,
                ).multiplier,
            )
            for item in agents.items
        )
        with self.session_factory() as session:
            session.execute(delete(ScoreRow).where(ScoreRow.run_id == run_id))
            session.add_all(
                [
                    ScoreRow(
                        run_id=run_id,
                        symbol=score.symbol,
                        trading_date=score.trading_date,
                        decision_at=score.decision_at,
                        fundamental_score=score.fundamental_score,
                        technical_score=score.technical_score,
                        sentiment_score=score.sentiment_score,
                        quality_confidence_score=score.quality_confidence_score,
                        base_total_score=score.base_total_score,
                        dividend_bonus=score.dividend_bonus,
                        event_risk_multiplier=score.event_risk_multiplier,
                        total_score=score.total_score,
                        formula_version=score.formula_version,
                        agent_bundle_sha256=score.agent_bundle_sha256,
                        evidence_bundle_sha256=score.evidence_bundle_sha256,
                        feature_snapshot_id=str(score.feature_snapshot_id),
                    )
                    for score in scores
                ]
            )
            session.commit()
        return self._write_stage(run_id, "scores", ScoreArtifact(scores=scores))

    def qlib_filter(self, run_id: str, score_snapshot_id: str) -> str:
        if score_snapshot_id != self._stage_digest(run_id, "scores"):
            raise ValueError("candidate stage received an unknown score artifact")
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        score_artifact = self._read_stage(run_id, "scores", ScoreArtifact)
        feature_artifact = self._read_stage(run_id, "features", FeatureArtifact)
        features = {item.symbol: item for item in feature_artifact.items}
        industries = {item.symbol: item for item in bundle.industries}
        universe = self._read_stage(run_id, "universe", UniverseResult)
        formal_eligible, _ = self._formal_eligibility(bundle, universe)
        formal_scores = tuple(
            score for score in score_artifact.scores if score.symbol in formal_eligible
        )
        ordered = sorted(formal_scores, key=lambda item: (item.total_score, item.symbol))
        percentiles = {
            item.symbol: (index + 1) / len(ordered) for index, item in enumerate(ordered)
        }
        legacy_event_policy = EventRiskPolicy(
            version="builtin-events-v1",
            multipliers={
                EventSeverity.LOW: 1.0,
                EventSeverity.MEDIUM: 0.8,
                EventSeverity.HIGH: 0.5,
                EventSeverity.CRITICAL: 0.0,
            },
            blocked_severities=frozenset({EventSeverity.CRITICAL}),
        )
        candidates = tuple(
            Candidate(
                symbol=score.symbol,
                trading_date=bundle.trading_date,
                decision_at=bundle.decision_at,
                total_score=score.total_score,
                base_total_score=score.base_total_score,
                dividend_bonus=score.dividend_bonus,
                prediction_percentile=percentiles[score.symbol],
                industry_code=industries[score.symbol].industry_code,
                industry_name=industries[score.symbol].industry_name,
                volatility=max(
                    features[score.symbol].technical.annualized_volatility_20d or 0.0,
                    0.01,
                ),
                event_risk_multiplier=(
                    score.event_risk_multiplier
                    if self.policy.scoring.formula_version == FORMULA_VERSION_V2
                    else aggregate_event_risk(
                        bundle.events_by_symbol.get(score.symbol, ()), legacy_event_policy
                    ).multiplier
                ),
                style_exposures=bundle.style_exposures[score.symbol],
            )
            for score in sorted(
                formal_scores,
                key=lambda item: (-item.total_score, item.symbol),
            )
        )
        score_by_symbol = {item.symbol: item for item in score_artifact.scores}
        with self.session_factory() as session:
            session.execute(delete(CandidateRow).where(CandidateRow.run_id == run_id))
            session.add_all(
                [
                    CandidateRow(
                        run_id=run_id,
                        symbol=candidate.symbol,
                        trading_date=candidate.trading_date,
                        decision_at=candidate.decision_at,
                        rank=index,
                        total_score=candidate.total_score,
                        prediction_percentile=candidate.prediction_percentile,
                        industry_code=candidate.industry_code,
                        industry_name=candidate.industry_name,
                        event_risk_multiplier=candidate.event_risk_multiplier,
                        style_exposures=candidate.style_exposures,
                        evidence_hash=score_by_symbol[candidate.symbol].evidence_bundle_sha256,
                    )
                    for index, candidate in enumerate(candidates, start=1)
                ]
            )
            session.commit()
        return self._write_stage(run_id, "candidates", CandidateArtifact(candidates=candidates))

    def _cumulative_backtest_signals(
        self,
        session: Session,
        run: JobRun,
        bundle: CanonicalDailyBundle,
    ) -> tuple[tuple[BacktestSignal, ...], set[str]]:
        calendar = {item.trading_date for item in bundle.bars}
        current_gate = run.manifest.get("data_quality_gate")
        current_gate = current_gate if isinstance(current_gate, dict) else {}
        current_symbols = set(current_gate.get("formal_eligible_symbols", ())) or {
            item.symbol for item in bundle.securities
        }
        rows = session.execute(
            select(CandidateRow, JobRun)
            .join(JobRun, CandidateRow.run_id == JobRun.run_id)
            .where(
                JobRun.user_id == run.user_id,
                JobRun.run_type == "DAILY",
                JobRun.trading_date.in_(calendar),
                JobRun.status.in_(("RUNNING", "SUCCEEDED")),
            )
            .order_by(JobRun.trading_date, JobRun.started_at.desc(), CandidateRow.rank)
        ).all()
        grouped: dict[tuple[str, date], list[CandidateRow]] = {}
        decisions: dict[tuple[str, date], datetime] = {}
        snapshot_hashes: dict[tuple[str, date], str] = {}
        dropped_symbols: set[str] = set()
        for candidate, job in rows:
            key = (job.run_id, job.trading_date)
            gate = job.manifest.get("data_quality_gate")
            gate = gate if isinstance(gate, dict) else {}
            if gate and int(gate.get("formal_eligible_count", 0)) < (
                self.policy.portfolio.target_count
            ):
                dropped_symbols.add(candidate.symbol)
                continue
            grouped.setdefault(key, []).append(candidate)
            decision = job.decision_at
            if decision.tzinfo is None:
                decision = decision.replace(tzinfo=SHANGHAI)
            decisions[key] = decision
            snapshot_hashes[key] = str(job.manifest.get("acquired_bundle_sha256") or job.input_hash)
        signals: list[BacktestSignal] = []
        chosen_dates: set[date] = set()
        for key, candidates_for_run in sorted(grouped.items(), key=lambda item: item[0][1]):
            if key[1] == run.trading_date and key[0] != run.run_id:
                continue
            if key[1] in chosen_dates:
                continue
            chosen_dates.add(key[1])
            selected = candidates_for_run[: self.policy.portfolio.target_count]
            available = [item for item in selected if item.symbol in current_symbols]
            dropped_symbols.update(
                item.symbol for item in selected if item.symbol not in current_symbols
            )
            if not available:
                continue
            target = (Decimal("1") - self.policy.portfolio.cash_buffer) / len(available)
            for candidate in available:
                signals.append(
                    BacktestSignal(
                        signal_date=candidate.trading_date,
                        decision_at=decisions[key],
                        snapshot_hash=snapshot_hashes[key],
                        symbol=candidate.symbol,
                        industry_code=candidate.industry_code,
                        target_weight=target,
                    )
                )
        return tuple(signals), dropped_symbols

    def risk_state(self, run_id: str) -> str:
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        universe = self._read_stage(run_id, "universe", UniverseResult)
        control = transition_drawdown_state(
            nav=bundle.nav,
            high_watermark=bundle.high_watermark,
            config=DrawdownConfig(
                warning_threshold=self.policy.risk.derisk_drawdown,
                fuse_threshold=self.policy.risk.observe_only_drawdown,
                derisk_gross_multiplier=Decimal("0.5"),
                minimum_observation_sessions=self.policy.risk.minimum_observation_sessions,
                recovery_threshold=self.policy.risk.recovery_drawdown,
            ),
            previous=bundle.previous_risk_state,
            manual_recovery_confirmed=(
                bundle.manual_recovery_confirmed
                if self.policy.risk.manual_recovery_required
                else True
            ),
        )
        formal_eligible, excluded = self._formal_eligibility(bundle, universe)
        reason_code: str | None = None
        reason_message: str | None = None
        if control.state == PortfolioRiskState.OBSERVE_ONLY:
            reason_code = "MAX_DRAWDOWN_FUSE_ACTIVE"
            reason_message = "最大回撤熔断仍在生效，需满足恢复阈值、观察期和人工恢复要求"
        _, _, manifest = self._run_context(run_id)
        research_scope = str(manifest.get("research_scope", "MARKET")).upper()
        portfolio_requested = bool(manifest.get("portfolio_requested", True))
        insufficient_for_portfolio = len(formal_eligible) < self.policy.portfolio.target_count
        if (
            control.state != PortfolioRiskState.OBSERVE_ONLY
            and insufficient_for_portfolio
            and research_scope == "MARKET"
        ):
            control = DrawdownControlState(
                state=PortfolioRiskState.OBSERVE_ONLY,
                drawdown=control.drawdown,
                observation_sessions=max(1, control.observation_sessions),
            )
            reason_code = "INSUFFICIENT_COMPLETE_SYMBOLS"
            reason_message = (
                f"正式可用股票仅 {len(formal_eligible)} 只，少于组合要求的 "
                f"{self.policy.portfolio.target_count} 只"
            )
        gate = {
            "minimum_required": self.policy.portfolio.target_count,
            "formal_eligible_count": len(formal_eligible),
            "formal_eligible_symbols": sorted(formal_eligible),
            "excluded_symbols": {symbol: list(reasons) for symbol, reasons in excluded.items()},
        }
        with self.session_factory() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(run_id)
            updated = dict(run.manifest)
            updated["data_quality_gate"] = gate
            updated["symbol_data_quality"] = {
                symbol: dict(bundle.data_quality.get(symbol, {})) for symbol in universe.included
            }
            updated["risk_outcome"] = {
                "state": control.state.value,
                "drawdown": str(control.drawdown),
                "reason_code": reason_code,
                "reason_message": reason_message,
            }
            if insufficient_for_portfolio and research_scope != "MARKET":
                updated["portfolio_outcome"] = {
                    "requested": portfolio_requested,
                    "eligible": False,
                    "generated": False,
                    "reason_code": "INSUFFICIENT_DIVERSIFICATION",
                    "reason_message": (
                        f"正式合格股票仅 {len(formal_eligible)} 只，少于组合要求的 "
                        f"{self.policy.portfolio.target_count} 只；不影响个股正式研究"
                    ),
                }
            else:
                outcome = updated.get("portfolio_outcome")
                outcome = dict(outcome) if isinstance(outcome, dict) else {}
                outcome.setdefault("requested", portfolio_requested)
                outcome.setdefault("eligible", portfolio_requested)
                updated["portfolio_outcome"] = outcome
            run.manifest = updated
            session.commit()
        self._write_stage(
            run_id,
            "risk",
            RiskArtifact(
                control=control,
                formal_eligible_symbols=tuple(sorted(formal_eligible)),
                excluded_symbols=excluded,
                reason_code=reason_code,
                reason_message=reason_message,
            ),
        )
        return control.state.value

    @staticmethod
    def _formal_eligibility(
        bundle: CanonicalDailyBundle, universe: UniverseResult
    ) -> tuple[set[str], dict[str, tuple[str, ...]]]:
        eligible: set[str] = set()
        excluded: dict[str, tuple[str, ...]] = {}
        for symbol in universe.included:
            quality = bundle.data_quality.get(symbol, {})
            reasons: list[str] = []
            if quality.get("fundamental_placeholder"):
                reasons.extend(str(item) for item in quality.get("fundamental_reason_codes", ()))
                if not quality.get("fundamental_reason_codes"):
                    reasons.append("FUNDAMENTAL_DATA_INCOMPLETE")
            if quality.get("sentiment_placeholder"):
                reasons.extend(str(item) for item in quality.get("sentiment_reason_codes", ()))
                if not quality.get("sentiment_reason_codes"):
                    reasons.append("DISCLOSURE_DATA_INCOMPLETE")
            if reasons:
                excluded[symbol] = tuple(dict.fromkeys(reasons))
            else:
                eligible.add(symbol)
        return eligible, excluded

    def portfolio_requested(self, run_id: str) -> bool:
        _, _, manifest = self._run_context(run_id)
        outcome = manifest.get("portfolio_outcome")
        outcome = outcome if isinstance(outcome, dict) else {}
        return (
            bool(manifest.get("portfolio_requested", True))
            and outcome.get("eligible", True) is not False
        )

    def build_portfolio(self, run_id: str, candidate_snapshot_id: str) -> str | None:
        if candidate_snapshot_id != self._stage_digest(run_id, "candidates"):
            raise ValueError("portfolio stage received an unknown candidate artifact")
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        artifact = self._read_stage(run_id, "candidates", CandidateArtifact)
        risk = self._read_stage(run_id, "risk", RiskArtifact).control
        latest_bars = {
            item.symbol: item for item in bundle.bars if item.trading_date == bundle.trading_date
        }
        _, _, manifest = self._run_context(run_id)
        raw_budget = manifest.get("research_budget")
        budget = raw_budget if isinstance(raw_budget, dict) else {}
        total_budget = (
            Decimal(str(budget["total_budget"]))
            if budget.get("total_budget") is not None
            else bundle.nav
        )
        per_symbol_budget = (
            Decimal(str(budget["per_symbol_budget"]))
            if budget.get("per_symbol_budget") is not None
            else None
        )
        max_stock_price = (
            Decimal(str(budget["max_stock_price"]))
            if budget.get("max_stock_price") is not None
            else None
        )
        quotes = tuple(
            CandidateQuote(
                candidate=candidate,
                reference_price=latest_bars[candidate.symbol].close,
                available_at=latest_bars[candidate.symbol].available_at,
                snapshot_hash=self._stage_digest(run_id, "bundle"),
                lot_size=100,
            )
            for candidate in artifact.candidates
            if max_stock_price is None or latest_bars[candidate.symbol].close <= max_stock_price
        )
        maximum_single_weight = self.policy.portfolio.maximum_single_weight
        if per_symbol_budget is not None:
            maximum_single_weight = min(
                maximum_single_weight,
                per_symbol_budget / total_budget,
            )
        result = PortfolioBuilder(
            PortfolioConfig(
                target_count=self.policy.portfolio.target_count,
                maximum_single_weight=maximum_single_weight,
                maximum_industry_weight=self.policy.portfolio.maximum_industry_weight,
                maximum_turnover=self.policy.portfolio.maximum_one_way_turnover,
                style_exposure_limits=self.policy.portfolio.style_exposure_limits,
                base_cash_weight=self.policy.portfolio.cash_buffer,
                minimum_prediction_percentile=Decimal("0"),
                constraint_version=(
                    f"{self.policy.version}-personal-budget-{stable_hash(budget)[:12]}"
                    if any(value is not None for value in budget.values())
                    else self.policy.version
                ),
                enforce_turnover_on_initial=False,
                allocation_tolerance=Decimal("0.000000001"),
                maximum_allocation_iterations=200,
            )
        ).build(
            quotes=quotes,
            nav=total_budget,
            effective_trading_date=bundle.next_trading_date,
            current_weights=bundle.current_weights,
            risk_state=risk.state,
            derisk_gross_multiplier=Decimal("0.5"),
        )
        if not result.success or result.portfolio is None:
            if any(value is not None for value in budget.values()):
                failure = (
                    result.failure.model_dump(mode="json")
                    if result.failure
                    else {
                        "code": "UNKNOWN",
                        "message": "预算约束下未生成模拟组合",
                        "details": {},
                    }
                )
                with self.session_factory() as session:
                    run = session.get(JobRun, run_id)
                    if run is None:
                        raise KeyError(run_id)
                    updated = dict(run.manifest)
                    updated["portfolio_outcome"] = {
                        "generated": False,
                        "reason": failure,
                        "eligible_price_count": len(quotes),
                    }
                    run.manifest = updated
                    session.commit()
                self._write_stage(run_id, "portfolio_constraints", failure)
                return None
            raise RuntimeError(f"builtin portfolio failed: {result.failure}")
        portfolio = result.portfolio
        persisted_portfolio_id = str(uuid5(NAMESPACE_URL, f"{run_id}:{portfolio.portfolio_id}"))
        with self.session_factory() as session:
            session.execute(delete(PortfolioRow).where(PortfolioRow.run_id == run_id))
            session.add(
                PortfolioRow(
                    portfolio_id=persisted_portfolio_id,
                    run_id=run_id,
                    trading_date=portfolio.trading_date,
                    effective_trading_date=portfolio.effective_trading_date,
                    status=portfolio.status.value,
                    expected_turnover=portfolio.expected_turnover,
                    cash_weight=portfolio.cash_weight,
                    constraint_version=portfolio.constraint_version,
                    input_hash=portfolio.input_hash,
                    positions=[item.model_dump(mode="json") for item in portfolio.positions],
                    rejection_reasons=[],
                )
            )
            session.flush()
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(run_id)
            updated = dict(run.manifest)
            updated["portfolio_outcome"] = {
                "requested": True,
                "eligible": True,
                "generated": True,
                "position_count": len(portfolio.positions),
                "nav": str(total_budget),
            }
            run.manifest = updated
            self._persist_cumulative_backtest_snapshot(
                session,
                run,
                bundle,
                observe_only=False,
            )
            session.commit()
        self._write_stage(run_id, "portfolio", portfolio)
        return persisted_portfolio_id

    def publish_report(self, run_id: str, portfolio_id: str | None, risk_state: str) -> str:
        bundle = self._read_stage(run_id, "bundle", CanonicalDailyBundle)
        candidates = self._read_stage(run_id, "candidates", CandidateArtifact)
        universe = self._read_stage(run_id, "universe", UniverseResult)
        scores = self._read_stage(run_id, "scores", ScoreArtifact)
        agents = self._read_stage(run_id, "agents", AgentArtifact)
        try:
            risk_artifact = self._read_stage(run_id, "risk", RiskArtifact)
            risk_reason_code = risk_artifact.reason_code
            risk_reason_message = risk_artifact.reason_message
            formal_eligible_symbols = risk_artifact.formal_eligible_symbols
            excluded_symbols = risk_artifact.excluded_symbols
        except KeyError:
            formal_eligible, excluded = self._formal_eligibility(bundle, universe)
            risk_reason_code = None
            risk_reason_message = None
            formal_eligible_symbols = tuple(sorted(formal_eligible))
            excluded_symbols = excluded
        positions: Sequence[Mapping[str, Any]] = ()
        if portfolio_id is not None:
            with self.session_factory() as session:
                row = session.get(PortfolioRow, portfolio_id)
                if row is None:
                    raise KeyError(portfolio_id)
                positions = tuple(row.positions)
        with self.session_factory() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(run_id)
            manifest = dict(run.manifest or {})
            quality_rows = [
                item
                for symbol, item in bundle.data_quality.items()
                if symbol in set(universe.included) and isinstance(item, dict)
            ]
            quality_summary = {
                "symbol_count": len(quality_rows),
                "fundamental_placeholder_count": sum(
                    bool(item.get("fundamental_placeholder")) for item in quality_rows
                ),
                "sentiment_placeholder_count": sum(
                    bool(item.get("sentiment_placeholder")) for item in quality_rows
                ),
                "industry_placeholder_count": sum(
                    bool(item.get("industry_placeholder")) for item in quality_rows
                ),
            }
            if risk_state != "OBSERVE_ONLY" and portfolio_id is None:
                try:
                    create_backtest_snapshot(
                        session=session,
                        run=run,
                        bundle=bundle,
                        lake_root=self._settings.lake_root,
                        policy=self.policy,
                        phase="SINGLE_SYMBOL_ADVICE",
                        dataset="backtest_bundle",
                        extra_details={
                            "snapshot_purpose": "SINGLE_SYMBOL_ADVICE",
                            "formal_eligible_symbols": list(formal_eligible_symbols),
                            "data_quality_gate": run.manifest.get("data_quality_gate", {}),
                            "risk_outcome": run.manifest.get("risk_outcome", {}),
                        },
                    )
                    updated = dict(run.manifest)
                    updated["advice_snapshot_outcome"] = {
                        "generated": True,
                        "purpose": "SINGLE_SYMBOL_ADVICE",
                    }
                    run.manifest = updated
                    manifest = updated
                except (RuntimeError, ValueError) as exc:
                    updated = dict(run.manifest)
                    updated["advice_snapshot_outcome"] = {
                        "generated": False,
                        "reason_code": "INSUFFICIENT_VALIDATION_HISTORY",
                        "reason_message": str(exc),
                    }
                    run.manifest = updated
                    manifest = updated
            if risk_state == "OBSERVE_ONLY":
                self._persist_cumulative_backtest_snapshot(
                    session,
                    run,
                    bundle,
                    observe_only=True,
                )
            existing = session.scalar(
                select(ReportRow).where(
                    ReportRow.run_id == run_id,
                    ReportRow.report_type == "DAILY_RESEARCH",
                )
            )
            if existing is not None:
                session.commit()
                return existing.report_id
            buy_execution_date = bundle.next_trading_date
            # Fail closed: without a later frozen calendar session the T+1 earliest
            # sell date stays None instead of a weekend+1 guess.
            t1_earliest_sell_date = next(
                (item for item in bundle.trading_calendar if item > bundle.next_trading_date),
                None,
            )
            report = DailyReportService(session, self.object_store).generate(
                run_id=run_id,
                trading_date=bundle.trading_date,
                context={
                    "trading_date": bundle.trading_date.isoformat(),
                    "decision_at": bundle.decision_at.isoformat(),
                    "run_status": "FUSED" if risk_state == "OBSERVE_ONLY" else "SUCCEEDED",
                    "fused": risk_state == "OBSERVE_ONLY",
                    "buy_execution_date": buy_execution_date.isoformat(),
                    "t1_earliest_sell_date": (
                        t1_earliest_sell_date.isoformat()
                        if t1_earliest_sell_date is not None
                        else None
                    ),
                    "trade_calendar_source": bundle.calendar_source,
                    "candidates": candidates.candidates,
                    "report_symbols": self._report_symbol_rows(
                        bundle=bundle,
                        scores=scores,
                        agents=agents,
                        candidates=candidates,
                        formal_eligible_symbols=formal_eligible_symbols,
                        excluded_symbols=excluded_symbols,
                        global_fused=risk_state == "OBSERVE_ONLY",
                        global_reason_code=risk_reason_code,
                    ),
                    "positions": positions,
                    "risks": [f"risk_state={risk_state}"],
                    "risk_reason_code": risk_reason_code,
                    "risk_reason_message": risk_reason_message,
                    "formal_eligible_symbols": formal_eligible_symbols,
                    "excluded_symbols": excluded_symbols,
                    "research_scope": manifest.get("research_scope", "MARKET"),
                    "target_symbols": manifest.get("target_symbols", []),
                    "research_budget": manifest.get("research_budget", {}),
                    "research_only_reason": manifest.get("research_only_reason"),
                    "portfolio_outcome": manifest.get("portfolio_outcome", {}),
                    "quality_summary": quality_summary,
                    "run_id": run_id,
                    "input_hash": run.input_hash,
                    "formula_version": self.policy.scoring.formula_version,
                    "trade_rule_version": RULESET_VERSION,
                },
            )
            session.commit()
        self._write_stage(
            run_id,
            "report",
            {
                "report_id": report.report_id,
                "uri": report.object_uri,
                "sha256": report.content_sha256,
            },
        )
        return report.report_id

    @staticmethod
    def _report_symbol_rows(
        *,
        bundle: CanonicalDailyBundle,
        scores: ScoreArtifact,
        agents: AgentArtifact,
        candidates: CandidateArtifact,
        formal_eligible_symbols: Sequence[str],
        excluded_symbols: Mapping[str, Sequence[str]],
        global_fused: bool,
        global_reason_code: str | None,
    ) -> list[dict[str, Any]]:
        securities = {item.symbol: item for item in bundle.securities}
        agent_by_symbol = {item.symbol: item for item in agents.items}
        candidate_by_symbol = {item.symbol: item for item in candidates.candidates}
        eligible = set(formal_eligible_symbols)
        rows: list[dict[str, Any]] = []
        for score in sorted(scores.scores, key=lambda item: (-item.total_score, item.symbol)):
            reasons = list(excluded_symbols.get(score.symbol, ()))
            if global_fused:
                reasons.append(global_reason_code or "GLOBAL_RISK_FUSE_ACTIVE")
            elif score.event_risk_multiplier <= 0:
                reasons.append("CRITICAL_EVENT_RISK")
            reasons = list(dict.fromkeys(reasons))
            advice_eligible = (
                not global_fused and score.symbol in eligible and score.event_risk_multiplier > 0
            )
            components = {
                item.component: {
                    "score": item.score,
                    "confidence": item.confidence,
                    "positive_factors": item.positive_factors,
                    "negative_factors": item.negative_factors,
                    "risk_flags": item.risk_flags,
                    "summary": component_summary(item.component, item.score, item.confidence),
                    "evidence": [
                        {
                            "source": evidence.source,
                            "source_record_id": evidence.source_record_id,
                            "available_at": evidence.available_at.isoformat(),
                            "excerpt": evidence.excerpt,
                        }
                        for evidence in item.evidence
                    ],
                }
                for item in agent_by_symbol[score.symbol].results
            }
            candidate = candidate_by_symbol.get(score.symbol)
            rows.append(
                {
                    "symbol": score.symbol,
                    "name": securities[score.symbol].short_name,
                    "score": score,
                    "candidate": candidate,
                    "components": components,
                    "data_quality": bundle.data_quality.get(score.symbol, {}),
                    "research_status": (
                        "FORMAL"
                        if advice_eligible
                        else "RISK_BLOCKED"
                        if global_fused or score.event_risk_multiplier <= 0
                        else "FORMAL_WITH_LIMITATIONS"
                    ),
                    "advice_eligible": advice_eligible,
                    "recommendation": None if advice_eligible else "NO_BUY",
                    "exclusion_reasons": reasons,
                    "plain_language_summary": symbol_summary(
                        total_score=score.total_score,
                        fundamental_score=score.fundamental_score,
                        technical_score=score.technical_score,
                        sentiment_score=score.sentiment_score,
                        advice_eligible=advice_eligible,
                        reasons=reasons,
                    ),
                }
            )
        return rows

    def _persist_cumulative_backtest_snapshot(
        self,
        session: Session,
        run: JobRun,
        bundle: CanonicalDailyBundle,
        *,
        observe_only: bool,
    ) -> None:
        signals, dropped_symbols = self._cumulative_backtest_signals(session, run, bundle)
        prior_bundle, prior_snapshot_id, prior_observe_only = self._latest_backtest_bundle(
            session, run
        )
        if not signals and prior_bundle is None:
            return
        create_backtest_snapshot(
            session=session,
            run=run,
            bundle=bundle,
            lake_root=self._settings.lake_root,
            policy=self.policy,
            signals=signals,
            phase="OBSERVE_ONLY_SIGNALS" if observe_only else "PORTFOLIO_SIGNALS",
            dataset="backtest_bundle",
            extra_details={
                "dropped_signal_symbols": sorted(dropped_symbols),
                "observe_only": observe_only or prior_observe_only,
                "current_run_observe_only": observe_only,
                "snapshot_purpose": "PORTFOLIO_BACKTEST",
                "run_status": "FUSED" if observe_only else "SUCCEEDED",
                "data_quality_gate": run.manifest.get("data_quality_gate", {}),
                "risk_outcome": run.manifest.get("risk_outcome", {}),
            },
            prior_bundle=prior_bundle,
            prior_snapshot_id=prior_snapshot_id,
        )

    def _latest_backtest_bundle(
        self,
        session: Session,
        run: JobRun,
    ) -> tuple[BacktestBundle | None, str | None, bool]:
        row = session.scalar(
            select(SnapshotManifestRow)
            .join(JobRun, SnapshotManifestRow.run_id == JobRun.run_id)
            .where(
                SnapshotManifestRow.dataset == "backtest_bundle",
                SnapshotManifestRow.status == "COMMITTED",
                JobRun.user_id == run.user_id,
                JobRun.run_type == "DAILY",
                JobRun.status.in_(("SUCCEEDED", "FUSED")),
                JobRun.trading_date < run.trading_date,
            )
            .order_by(JobRun.trading_date.desc(), SnapshotManifestRow.committed_at.desc())
        )
        if row is None:
            return None, None, False
        try:
            file_hash = str(row.details["parquet_file_sha256"])
        except KeyError as exc:
            raise RuntimeError("prior backtest snapshot lacks verified file hash") from exc
        return (
            read_backtest_bundle(
                {row.snapshot_id: row.parquet_uri},
                {row.snapshot_id: file_hash},
            ),
            row.snapshot_id,
            bool(row.details.get("observe_only", False)),
        )

    def state_for_run(self, run_id: str) -> dict[str, dict[str, str]]:
        return self._load_state(run_id)

    def _load_source_bundle(
        self,
        trading_date: date,
        decision_at: datetime,
        *,
        run_id: str | None = None,
        required_symbols: tuple[str, ...] = (),
    ) -> CanonicalDailyBundle:
        configured = os.environ.get("ASHARE_CANONICAL_BUNDLE")
        reused = self._load_reused_bundle(trading_date, decision_at, run_id=run_id)
        if reused is not None:
            bundle = reused
        elif configured:
            stripped = configured.strip()
            payload = (
                stripped if stripped.startswith("{") else Path(stripped).read_text(encoding="utf-8")
            )
            bundle = CanonicalDailyBundle.model_validate_json(payload)
        elif self._settings.canonical_bundle_mode == "demo":
            if not self._allow_demo_data:
                raise RuntimeError("demo canonical data requires ALLOW_DEMO_DATA=true")
            bundle = make_demo_bundle(trading_date)
        elif self._settings.canonical_bundle_mode == "file":
            raise RuntimeError("canonical_bundle_mode=file requires ASHARE_CANONICAL_BUNDLE")
        elif self._settings.canonical_bundle_mode == "akshare":
            builder = self._canonical_builder
            if builder is None:
                provider: CanonicalMarketProvider = AKShareCanonicalProvider(
                    max_attempts=self._settings.akshare_fetch_max_attempts,
                    backoff_seconds=self._settings.akshare_fetch_backoff_seconds,
                )
                if self._settings.tushare_token:
                    provider = FallbackCanonicalProvider(
                        provider,
                        TushareCanonicalProvider(self._settings.tushare_token),
                        minimum_history_rows=self._settings.akshare_history_sessions,
                    )
                builder = AKShareCanonicalBundleBuilder(
                    provider=provider,
                    bundle_size=self._settings.akshare_bundle_size,
                    history_sessions=self._settings.akshare_history_sessions,
                    news_window_days=self.policy.scoring.news_window_days,
                )
            try:
                bundle = builder.build(
                    trading_date,
                    decision_at,
                    required_symbols=required_symbols,
                )
            except MarketDataAcquisitionError as exc:
                if run_id is not None:
                    self._record_akshare_acquisition_events(run_id, builder, fatal=exc)
                raise
            except Exception:
                if run_id is not None:
                    self._record_akshare_acquisition_events(run_id, builder)
                raise
            else:
                if run_id is not None:
                    self._record_akshare_acquisition_events(run_id, builder)
        else:
            raise RuntimeError("unsupported canonical bundle mode")
        if bundle.trading_date != trading_date or bundle.decision_at != decision_at:
            raise ValueError("canonical bundle does not match requested trading_date/decision_at")
        return bundle

    def _load_reused_bundle(
        self,
        trading_date: date,
        decision_at: datetime,
        *,
        run_id: str | None,
    ) -> CanonicalDailyBundle | None:
        if run_id is None:
            return None
        with self.session_factory() as session:
            current = session.get(JobRun, run_id)
            if current is None:
                raise KeyError(run_id)
            source_id = (current.manifest or {}).get("source_bundle_run_id")
            if not isinstance(source_id, str) or not source_id:
                return None
            source = session.get(JobRun, source_id)
            if source is None or source.run_type != "DAILY":
                raise RuntimeError("historical research source bundle is unavailable")
            if current.user_id is None or source.user_id != current.user_id:
                raise RuntimeError("historical research source bundle ownership mismatch")
            expected_digest = (source.manifest or {}).get("acquired_bundle_sha256")
            if not isinstance(expected_digest, str) or len(expected_digest) != 64:
                raise RuntimeError("historical research source bundle is not immutable")

        actual_digest = self._stage_digest(source_id, "bundle")
        if actual_digest != expected_digest:
            raise RuntimeError("historical research source bundle hash mismatch")
        bundle = self._read_stage(source_id, "bundle", CanonicalDailyBundle)
        if bundle.trading_date != trading_date:
            raise RuntimeError("historical research source bundle date mismatch")
        if bundle.decision_at > decision_at:
            raise RuntimeError(
                "historical research source bundle is newer than the requested decision"
            )
        # The original bundle was validated against its own decision point. A
        # later historical decision can safely reuse it, but its bundle-level
        # decision_at must match the new run for downstream PIT checks.
        return bundle.model_copy(update={"decision_at": decision_at})

    def _record_akshare_acquisition_events(
        self,
        run_id: str,
        builder: AKShareCanonicalBundleBuilder,
        *,
        fatal: MarketDataAcquisitionError | None = None,
    ) -> None:
        events = [dict(item) for item in getattr(builder, "acquisition_events", ())]
        if fatal is not None:
            events.append({**fatal.audit_details(), "outcome": "failed_required_request"})
        if not events:
            return
        with self.session_factory() as session:
            audit = AuditLogger(session)
            for details in events:
                skipped = details.get("outcome") == "skipped_nonessential_symbol"
                audit.record(
                    run_id,
                    "AKSHARE_FETCH_SKIPPED" if skipped else "AKSHARE_FETCH_FAILED",
                    (
                        "A nonessential security was skipped after bounded provider attempts"
                        if skipped
                        else (
                            "A required canonical market-data request failed after bounded attempts"
                        )
                    ),
                    severity="WARNING" if skipped else "ERROR",
                    details=details,
                )
            session.commit()

    def _run_context(self, run_id: str) -> tuple[date, datetime, dict[str, Any]]:
        with self.session_factory() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(run_id)
            decision_at = run.decision_at
            if decision_at.tzinfo is None or decision_at.utcoffset() is None:
                decision_at = decision_at.replace(tzinfo=SHANGHAI)
            else:
                decision_at = decision_at.astimezone(SHANGHAI)
            return run.trading_date, decision_at, dict(run.manifest)

    def _state_path(self, run_id: str) -> Path:
        return self.state_root / f"{stable_hash(run_id)}.json"

    def _load_state(self, run_id: str) -> dict[str, dict[str, str]]:
        path = self._state_path(run_id)
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("builtin state index must contain an object")
        return {
            str(key): {str(inner_key): str(inner_value) for inner_key, inner_value in item.items()}
            for key, item in value.items()
            if isinstance(item, dict)
        }

    def _index_stage(self, run_id: str, stage: str, *, uri: str, digest: str) -> None:
        state = self._load_state(run_id)
        state[stage] = {"uri": uri, "sha256": digest}
        path = self._state_path(run_id)
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_bytes(canonical_json(state))
        os.replace(temp, path)

    def _write_stage(self, run_id: str, stage: str, value: Any) -> str:
        uri, digest = self.object_store.put(canonical_json(value), content_type="application/json")
        self._index_stage(run_id, stage, uri=uri, digest=digest)
        return digest

    def _read_stage(self, run_id: str, stage: str, model: type[M]) -> M:
        state = self._load_state(run_id)
        try:
            uri = state[stage]["uri"]
        except KeyError as exc:
            raise KeyError(f"missing builtin stage artifact: {run_id}/{stage}") from exc
        payload = self.object_store.get(uri)
        return model.model_validate_json(payload)

    def _stage_digest(self, run_id: str, stage: str) -> str:
        try:
            return self._load_state(run_id)[stage]["sha256"]
        except KeyError as exc:
            raise KeyError(f"missing builtin stage digest: {run_id}/{stage}") from exc

    def _uri_for_digest(self, digest: str) -> str:
        return (self.object_root / "sha256" / digest[:2] / digest).resolve().as_uri()

    def _resolve_llm_client(self, run_id: str) -> StructuredLLMClient | None:
        if self._injected_llm_client is not None:
            return self._injected_llm_client
        from ashare_ai.agents.model_settings import ModelConfigurationService
        from ashare_ai.agents.openai_compatible import OpenAICompatibleStructuredLLMClient

        with self.session_factory() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(run_id)
            reference = run.manifest.get("model_configuration")
            if not isinstance(reference, dict) or not reference.get("enabled", False):
                return None
            runtime = ModelConfigurationService(self._settings).resolve_pinned(session, reference)
        if runtime is None:
            return None
        return OpenAICompatibleStructuredLLMClient(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            model=runtime.research_model,
            reasoning_effort=runtime.research_reasoning_effort,
            timeout_seconds=runtime.timeout_seconds,
            cache_policy=runtime.profile_for(runtime.research_model).cache_policy,
        )

    def _run_llm_component(
        self,
        component: Literal["fundamental", "technical", "sentiment"],
        symbol: str,
        results: dict[tuple[str, str], AgentComponentResult],
    ) -> AgentComponentResult:
        return results[(symbol, component)]

    def _run_llm_components(
        self,
        client: StructuredLLMClient,
        decision_at: datetime,
        feature_items: Sequence[SymbolFeatureSet],
        bundle: CanonicalDailyBundle,
    ) -> dict[tuple[str, str], AgentComponentResult]:
        requests: list[
            tuple[str, Literal["fundamental", "technical", "sentiment"], FrozenModel, EvidenceRef]
        ] = []
        for feature_item in feature_items:
            evidence = self._evidence_for_symbol(bundle, feature_item.symbol)
            quality = bundle.data_quality[feature_item.symbol]
            if not quality.get("fundamental_placeholder"):
                requests.append(
                    (feature_item.symbol, "fundamental", feature_item.fundamental, evidence[0])
                )
            requests.append(
                (feature_item.symbol, "technical", feature_item.technical, evidence[1])
            )
            if not quality.get("sentiment_placeholder"):
                requests.append(
                    (feature_item.symbol, "sentiment", feature_item.sentiment, evidence[2])
                )
        return _run_async(self._run_llm_components_async(client, decision_at, requests))

    async def _run_llm_components_async(
        self,
        client: StructuredLLMClient,
        decision_at: datetime,
        components: Sequence[
            tuple[str, Literal["fundamental", "technical", "sentiment"], FrozenModel, EvidenceRef]
        ],
    ) -> dict[tuple[str, str], AgentComponentResult]:
        for _, _, _, evidence in components:
            if evidence.available_at > decision_at:
                raise ValueError("component request cannot include future evidence")
        semaphore = asyncio.Semaphore(self._settings.llm_agent_max_concurrency)

        async def run_one(
            symbol: str,
            component: Literal["fundamental", "technical", "sentiment"],
            features: FrozenModel,
            evidence: EvidenceRef,
        ) -> tuple[tuple[str, str], AgentComponentResult]:
            request = AgentRequest(
                component=component,
                symbol=symbol,
                decision_at=decision_at,
                prompt_version="builtin-llm-v2",
                features=self._scalar_features(features),
                evidence=(evidence,),
            )
            async with semaphore:
                result = await run_component_agent(
                    client,
                    request,
                    system_instruction=_COMPONENT_SYSTEM_INSTRUCTIONS[component],
                )
            return (symbol, component), result

        completed: list[tuple[tuple[str, str], AgentComponentResult]] = []
        batch_size = max(1, self._settings.llm_agent_max_concurrency)
        for offset in range(0, len(components), batch_size):
            completed.extend(
                await asyncio.gather(
                    *(
                        run_one(symbol, component, features, evidence)
                        for symbol, component, features, evidence in components[
                            offset : offset + batch_size
                        ]
                    )
                )
            )
        return dict(completed)

    def _record_llm_degradation(self, run_id: str, error: Exception) -> None:
        details: dict[str, Any] = {
            "error_type": type(error).__name__,
            "error_code": getattr(error, "code", None),
            "status_code": getattr(error, "status_code", None),
            "retryable": bool(getattr(error, "retryable", False)),
            "fallback": "builtin-deterministic",
        }
        with self.session_factory() as session:
            run = session.get(JobRun, run_id)
            if run is not None:
                run.manifest = {
                    **dict(run.manifest),
                    "agent_backend_effective": "builtin-deterministic-fallback",
                    "agent_backend_degradation": details,
                }
                AuditLogger(session).record(
                    run_id,
                    "LLM_AGENT_DEGRADED",
                    "Model Agent unavailable; deterministic Agent mapping was used",
                    severity="WARNING",
                    details=details,
                )
                session.commit()

    @staticmethod
    def _scalar_features(features: FrozenModel) -> dict[str, float | int | str | bool | None]:
        values: dict[str, float | int | str | bool | None] = {}
        for name, value in features.model_dump().items():
            if isinstance(value, float) and not math.isfinite(value):
                continue
            if value is None or isinstance(value, (str, bool, int, float)):
                values[name] = value
        return values

    @staticmethod
    def _agent_result(
        component: Literal["fundamental", "technical", "sentiment"],
        score: float,
        confidence: float,
        evidence: EvidenceRef,
        features: FrozenModel,
    ) -> AgentComponentResult:
        prompt_sha = stable_hash(
            {"model": "builtin-deterministic", "component": component, "features": features}
        )
        response_sha = stable_hash(
            {"component": component, "score": score, "confidence": confidence, "evidence": evidence}
        )
        return AgentComponentResult(
            component=component,
            score=score,
            confidence=max(0.0, min(1.0, confidence)),
            evidence=(evidence,),
            positive_factors=("系统根据已冻结特征完成确定性映射",),
            risk_flags=(),
            model_provider="builtin",
            model_name="builtin-deterministic",
            reasoning_effort="deterministic",
            prompt_version="builtin-v1",
            prompt_sha256=prompt_sha,
            response_sha256=response_sha,
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
            retry_count=0,
        )

    @staticmethod
    def _fundamental_score(features: FundamentalFeatures) -> float:
        return _bounded(
            55
            + 80 * (features.revenue_growth_yoy or 0)
            + 80 * (features.net_profit_growth_yoy or 0)
            + 30 * (features.return_on_equity or 0)
            - 20 * (features.debt_to_assets or 0)
        )

    @staticmethod
    def _technical_score(features: TechnicalFeatures) -> float:
        return _bounded(
            55
            + 120 * (features.return_20d or 0)
            + 80 * (features.close_to_ma20 or 0)
            - 15 * (features.annualized_volatility_20d or 0)
        )

    @staticmethod
    def _sentiment_score(features: SentimentFeatures) -> float:
        return _bounded(55 + 35 * features.tone_score - 40 * features.event_risk_ratio)

    @staticmethod
    def _evidence_for_symbol(
        bundle: CanonicalDailyBundle, symbol: str
    ) -> tuple[EvidenceRef, EvidenceRef, EvidenceRef]:
        financial = max(
            (item for item in bundle.financial_facts if item.symbol == symbol),
            key=lambda item: (item.report_period_end, item.available_at, item.source_record_id),
        )
        bar = max(
            (item for item in bundle.bars if item.symbol == symbol),
            key=lambda item: (item.trading_date, item.available_at, item.source_record_id),
        )
        document: PointInTimeRecord = max(
            (item for item in (*bundle.disclosures, *bundle.news) if item.symbol == symbol),
            key=lambda item: (item.available_at, item.source_record_id),
        )
        return (
            _evidence_ref(financial),
            _evidence_ref(bar),
            _evidence_ref(document),
        )


def _evidence_ref(record: PointInTimeRecord) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=f"{record.source}:{record.source_record_id}",
        evidence_type=record.__class__.__name__,
        source=record.source,
        source_record_id=record.source_record_id,
        available_at=record.available_at,
        payload_sha256=record.payload_sha256,
    )


def _run_async(coroutine: Coroutine[Any, Any, T]) -> T:
    """Bridge sync pipeline stages to async clients without nesting event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[T] = []
    failure: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # Preserve the client exception at the sync boundary.
            failure.append(exc)

    thread = threading.Thread(target=runner, name="ashare-llm-component", daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


def _is_degradable_llm_error(error: Exception) -> bool:
    from ashare_ai.agents.openai_compatible import OpenAICompatibleError

    if isinstance(error, (OpenAICompatibleError, OSError)):
        return True
    return isinstance(error, RuntimeError) and str(error).strip() == "can't start new thread"


def _bounded(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 6)


def create_backend() -> BuiltinDailyBackend:
    return BuiltinDailyBackend()
