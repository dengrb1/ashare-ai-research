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
    user_session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    client_ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[UserAccount] = relationship()


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
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    formula_version: Mapped[str] = mapped_column(String(32), nullable=False)
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
