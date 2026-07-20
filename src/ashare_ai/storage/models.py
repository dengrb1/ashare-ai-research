from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ashare_ai.core.hashing import stable_hash


def new_uuid() -> str:
    return str(uuid4())


def trading_rule_selector_key(context: Any) -> str:
    values = context.get_current_parameters()
    selector_fields = (
        "rule_type",
        "exchange",
        "market",
        "board",
        "security_type",
        "symbol",
        "risk_status",
        "is_st",
        "special_phase",
        "listing_session_from",
        "listing_session_to",
        "min_listing_days",
        "max_listing_days",
        "effective_from",
        "effective_to",
        "published_at",
        "source_type",
        "source_uri",
        "raw_payload_sha256",
    )
    return stable_hash({field: values.get(field) for field in selector_fields})


class Base(DeclarativeBase):
    pass


class UserAccount(Base):
    __tablename__ = "user_accounts"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_session_active", "user_id", "expires_at", "revoked_at"),)

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.user_id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    session_type: Mapped[str] = mapped_column(String(16), nullable=False, default="WEB")
    refresh_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    client_ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[UserAccount] = relationship()


class UserAssetState(Base):
    """User-owned editable watchlist and simulated position state."""

    __tablename__ = "user_asset_states"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.user_id", ondelete="CASCADE"), primary_key=True
    )
    watchlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    positions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    exit_monitor_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_profit_trigger: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserResearchPreference(Base):
    """Per-user automatic daily-research preference; automatic runs are opt-in."""

    __tablename__ = "user_research_preferences"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.user_id", ondelete="CASCADE"), primary_key=True
    )
    auto_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelConfigurationVersion(Base):
    """Immutable, encrypted model configuration revision."""

    __tablename__ = "model_configuration_versions"
    __table_args__ = (
        UniqueConstraint("version", name="uq_model_configuration_version"),
        UniqueConstraint("config_sha256", name="uq_model_configuration_hash"),
    )

    configuration_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    search_model: Mapped[str] = mapped_column(String(128), nullable=False)
    search_reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False)
    research_model: Mapped[str] = mapped_column(String(128), nullable=False)
    research_reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("user_accounts.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActiveModelConfiguration(Base):
    """Mutable pointer and health state for the active immutable revision."""

    __tablename__ = "active_model_configuration"

    scope: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    configuration_id: Mapped[str] = mapped_column(
        ForeignKey("model_configuration_versions.configuration_id"), nullable=False
    )
    activated_by: Mapped[str | None] = mapped_column(ForeignKey("user_accounts.user_id"))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNTESTED")
    last_check_message: Mapped[str | None] = mapped_column(Text)

    configuration: Mapped[ModelConfigurationVersion] = relationship()


class SecurityMaster(Base):
    __tablename__ = "security_master"
    __table_args__ = (
        UniqueConstraint("symbol", "effective_from", "source", name="uq_security_effective"),
        Index("ix_security_asof", "symbol", "available_at", "effective_from"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False, index=True)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    board: Mapped[str] = mapped_column(String(16), nullable=False)
    short_name: Mapped[str] = mapped_column(String(64), nullable=False)
    list_date: Mapped[date] = mapped_column(Date, nullable=False)
    delist_date: Mapped[date | None] = mapped_column(Date)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    industry_code: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    ingestion_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    availability_basis: Mapped[str] = mapped_column(String(32), nullable=False)


class SnapshotManifestRow(Base):
    __tablename__ = "snapshot_manifests"
    __table_args__ = (
        UniqueConstraint(
            "dataset",
            "source",
            "schema_version",
            "adapter_version",
            "payload_sha256",
            name="uq_snapshot_versioned_hash",
        ),
        Index("ix_snapshot_committed", "dataset", "status", "fetched_at"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("job_runs.run_id"), index=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parquet_uri: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObjectManifestRow(Base):
    __tablename__ = "object_manifests"
    __table_args__ = (UniqueConstraint("content_sha256", name="uq_object_content_hash"),)

    object_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)

    occurrences: Mapped[list[ObjectOccurrenceRow]] = relationship(
        back_populates="object_manifest", cascade="all, delete-orphan"
    )


class ObjectOccurrenceRow(Base):
    __tablename__ = "object_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "object_id",
            "source",
            "source_record_id",
            "fetched_at",
            name="uq_object_occurrence",
        ),
        Index("ix_object_occurrence_source", "source", "source_record_id", "fetched_at"),
    )

    occurrence_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    object_id: Mapped[str] = mapped_column(ForeignKey("object_manifests.object_id"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    object_manifest: Mapped[ObjectManifestRow] = relationship(back_populates="occurrences")


class TradingRuleRow(Base):
    __tablename__ = "trading_rules"
    __table_args__ = (
        UniqueConstraint("rule_version", "selector_key", name="uq_rule_version_selector"),
        Index("ix_rule_match", "effective_from", "effective_to", "market", "board", "is_st"),
    )

    rule_id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_uuid)
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPOSITE")
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    selector_key: Mapped[str] = mapped_column(
        String(64), nullable=False, default=trading_rule_selector_key
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(8))
    market: Mapped[str | None] = mapped_column(String(8))
    board: Mapped[str | None] = mapped_column(String(16))
    security_type: Mapped[str | None] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(9))
    risk_status: Mapped[str | None] = mapped_column(String(32))
    is_st: Mapped[bool | None] = mapped_column(Boolean)
    special_phase: Mapped[str | None] = mapped_column(String(32))
    listing_session_from: Mapped[int | None] = mapped_column(Integer)
    listing_session_to: Mapped[int | None] = mapped_column(Integer)
    min_listing_days: Mapped[int | None] = mapped_column(Integer)
    max_listing_days: Mapped[int | None] = mapped_column(Integer)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fallback_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    price_limit_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    no_price_limit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    t_plus_one: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stamp_tax_rate: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False, default=0)
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False, default=0)
    minimum_commission: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=0)
    transfer_fee_rate: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False, default=0)
    source_uri: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String(64))
    raw_payload_sha256: Mapped[str | None] = mapped_column(String(64))
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_job_idempotency"),
        Index("ix_job_trading_date", "trading_date", "run_type", "status"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("user_accounts.user_id"), index=True)
    active_research_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class AgentCall(Base):
    __tablename__ = "agent_calls"
    __table_args__ = (Index("ix_agent_run_component", "run_id", "component"),)

    call_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_runs.run_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    component: Mapped[str] = mapped_column(String(16), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvidenceRow(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "symbol",
            "component",
            "evidence_id",
            name="uq_evidence_run_component_id",
        ),
        Index("ix_evidence_lineage", "run_id", "symbol", "component"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    evidence_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_runs.run_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    component: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    object_occurrence_id: Mapped[str | None] = mapped_column(
        ForeignKey("object_occurrences.occurrence_id")
    )
    object_uri: Mapped[str | None] = mapped_column(Text)


class ScoreRow(Base):
    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("run_id", "symbol", name="uq_score_run_symbol"),
        Index("ix_score_date_total", "trading_date", "total_score"),
    )

    score_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_runs.run_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fundamental_score: Mapped[float] = mapped_column(Float, nullable=False)
    technical_score: Mapped[float] = mapped_column(Float, nullable=False)
    sentiment_score: Mapped[float] = mapped_column(Float, nullable=False)
    quality_confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    base_total_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    dividend_bonus: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    event_risk_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    formula_version: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)


class CandidateRow(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "symbol", name="uq_candidate_run_symbol"),
        Index("ix_candidate_date_rank", "trading_date", "rank"),
    )

    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_runs.run_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    prediction_percentile: Mapped[float] = mapped_column(Float, nullable=False)
    industry_code: Mapped[str] = mapped_column(String(32), nullable=False)
    event_risk_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    style_exposures: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PortfolioRow(Base):
    __tablename__ = "portfolios"
    __table_args__ = (
        UniqueConstraint("run_id", "effective_trading_date", name="uq_portfolio_run_date"),
    )

    portfolio_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_runs.run_id"), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_turnover: Mapped[float] = mapped_column(Float, nullable=False)
    cash_weight: Mapped[float] = mapped_column(Float, nullable=False)
    constraint_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    positions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    rejection_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    backtest_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("user_accounts.user_id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("job_runs.run_id"))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    artifacts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportRow(Base):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("run_id", "report_type", name="uq_report_run_type"),)

    report_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_runs.run_id"), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    report_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradePlanRow(Base):
    __tablename__ = "trade_plans"
    __table_args__ = (
        Index("ix_trade_plan_user_report_created", "user_id", "report_id", "created_at"),
        UniqueConstraint("active_trade_plan_key", name="uq_active_trade_plan_key"),
    )

    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.report_id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_runs.run_id"), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    objective: Mapped[str] = mapped_column(String(32), nullable=False)
    symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    budget_override: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    optimizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model_configuration: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    deterministic_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ai_explanation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    active_trade_plan_key: Mapped[str | None] = mapped_column(String(64), index=True)
    object_uri: Mapped[str | None] = mapped_column(Text)
    object_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class ExitAdviceRow(Base):
    """A user-owned, point-in-time exit review and validated simulated sell ladder."""

    __tablename__ = "exit_advice"
    __table_args__ = (
        Index("ix_exit_advice_user_created", "user_id", "created_at"),
        UniqueConstraint("input_hash", name="uq_exit_advice_input_hash"),
    )

    advice_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(9), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str | None] = mapped_column(String(16))
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unrealized_profit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    trigger_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    position_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    research_context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    model_name: Mapped[str | None] = mapped_column(String(128))
    reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIResponseCacheRow(Base):
    """Per-user exact-input cache for paid model generations."""

    __tablename__ = "ai_response_cache"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "purpose", "request_sha256", name="uq_ai_cache_user_purpose_request"
        ),
        Index("ix_ai_cache_expires", "expires_at"),
    )

    cache_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.user_id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AIChatThread(Base):
    __tablename__ = "ai_chat_threads"
    __table_args__ = (Index("ix_ai_chat_thread_user_updated", "user_id", "updated_at"),)

    thread_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AIChatMessage(Base):
    __tablename__ = "ai_chat_messages"
    __table_args__ = (Index("ix_ai_chat_message_thread_created", "thread_id", "created_at"),)

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("ai_chat_threads.thread_id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mentioned_symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    model_name: Mapped[str | None] = mapped_column(String(128))
    reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    context_sha256: Mapped[str | None] = mapped_column(String(64))
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_run_created", "run_id", "created_at"),)

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_runs.run_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    run: Mapped[JobRun] = relationship(back_populates="audit_events")
