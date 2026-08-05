from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ashare_ai.core.hashing import stable_hash

CanonicalSymbol = Annotated[
    str, StringConstraints(pattern=r"^\d{6}\.(SH|SZ|BJ)$", strip_whitespace=True)
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Score100 = Annotated[float, Field(ge=0, le=100)]
Confidence = Annotated[float, Field(ge=0, le=1)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Exchange(StrEnum):
    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"


class Board(StrEnum):
    MAIN = "MAIN"
    STAR = "STAR"
    CHINEXT = "CHINEXT"
    BSE = "BSE"
    OTHER = "OTHER"


class SecurityType(StrEnum):
    STOCK = "STOCK"
    ETF = "ETF"
    INDEX = "INDEX"


class TradeStatus(StrEnum):
    TRADING = "TRADING"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


class AvailabilityBasis(StrEnum):
    OFFICIAL_TIMESTAMP = "OFFICIAL_TIMESTAMP"
    VENDOR_TIMESTAMP = "VENDOR_TIMESTAMP"
    DATE_ONLY_CONSERVATIVE = "DATE_ONLY_CONSERVATIVE"
    FIRST_OBSERVED = "FIRST_OBSERVED"
    DERIVED = "DERIVED"


class SnapshotStatus(StrEnum):
    STAGING = "STAGING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    FUSED = "FUSED"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PointInTimeRecord(FrozenModel):
    symbol: CanonicalSymbol
    trading_date: date
    available_at: AwareDatetime
    source: str
    source_record_id: str
    fetched_at: AwareDatetime
    payload_sha256: Sha256
    schema_version: str = "1.0.0"
    adapter_version: str
    ingestion_run_id: UUID
    availability_basis: AvailabilityBasis

    @model_validator(mode="after")
    def fetched_after_available_for_first_observation(self) -> PointInTimeRecord:
        if (
            self.availability_basis == AvailabilityBasis.FIRST_OBSERVED
            and self.available_at != self.fetched_at
        ):
            raise ValueError("FIRST_OBSERVED requires available_at == fetched_at")
        return self


class SecurityMasterRecord(PointInTimeRecord):
    exchange: Exchange
    board: Board
    security_type: SecurityType = SecurityType.STOCK
    short_name: str
    list_date: date
    delist_date: date | None = None
    effective_from: date
    effective_to: date | None = None


class SecurityStatusRecord(PointInTimeRecord):
    is_st: bool
    is_suspended: bool
    status_codes: tuple[str, ...] = ()
    effective_from: date
    effective_to: date | None = None


class IndustryMembership(PointInTimeRecord):
    taxonomy: str
    taxonomy_version: str
    industry_code: str
    industry_name: str
    effective_from: date
    effective_to: date | None = None


class DailyBar(PointInTimeRecord):
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    amount: Decimal = Field(ge=0)
    prev_close: Decimal | None = Field(default=None, gt=0)
    trade_status: TradeStatus = TradeStatus.TRADING
    price_basis: Literal["RAW"] = "RAW"

    @model_validator(mode="after")
    def validate_ohlc(self) -> DailyBar:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below OHLC values")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above OHLC values")
        return self


class AdjustmentFactor(PointInTimeRecord):
    factor: Decimal = Field(gt=0)
    factor_method: str
    corporate_action_id: str | None = None


class FinancialFact(PointInTimeRecord):
    statement_type: str
    report_period_end: date
    report_type: str
    fiscal_year: int
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    field_code: str
    value: Decimal | None
    unit: str
    currency: str = "CNY"
    revision_seq: int = Field(ge=0)
    announcement_id: str | None = None


class Disclosure(PointInTimeRecord):
    announcement_id: str
    title: str
    category_codes: tuple[str, ...] = ()
    published_at: AwareDatetime | None = None
    official_verified: bool = False
    official_source: str | None = None
    document_uri: str
    document_sha256: Sha256

    @model_validator(mode="after")
    def require_official_source(self) -> Disclosure:
        if self.official_verified and not self.official_source:
            raise ValueError("official_verified disclosure requires official_source")
        return self


class NewsItem(PointInTimeRecord):
    news_id: str
    title: str
    published_at: AwareDatetime | None = None
    publisher: str
    body_uri: str | None = None
    content_sha256: Sha256
    related_symbols: tuple[CanonicalSymbol, ...]
    official_verified: bool = False
    source_uri: str | None = None


class CashDividend(PointInTimeRecord):
    """Implemented cash dividend that was knowable at the decision time."""

    dividend_id: str
    fiscal_year: int
    implementation_announcement_date: date
    record_date: date | None = None
    ex_dividend_date: date | None = None
    payment_date: date
    cash_dividend_per_share: Decimal = Field(gt=0)
    currency: str = "CNY"
    official_verified: bool = False
    source_uri: str | None = None


class FeatureValue(PointInTimeRecord):
    feature_set_version: str
    feature_name: str
    value: float | None
    input_manifest_id: UUID
    calculation_code_hash: Sha256


class ModelPrediction(PointInTimeRecord):
    model_version: str
    training_cutoff: AwareDatetime
    label_horizon: int = Field(gt=0)
    raw_prediction: float
    cross_sectional_percentile: float = Field(ge=0, le=1)
    model_artifact_sha256: Sha256
    dataset_manifest_id: UUID


class EvidenceRef(FrozenModel):
    evidence_id: str
    evidence_type: str
    source: str
    source_record_id: str
    available_at: AwareDatetime
    payload_sha256: Sha256
    excerpt: str | None = None


class AgentComponentResult(FrozenModel):
    component: Literal["fundamental", "technical", "sentiment"]
    score: Score100
    confidence: Confidence
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    positive_factors: tuple[str, ...] = ()
    negative_factors: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    model_provider: str
    model_name: str
    reasoning_effort: str
    prompt_version: str
    prompt_sha256: Sha256
    response_sha256: Sha256
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cache_policy: Literal["GROK", "OPENAI", "COMPATIBLE"] = "COMPATIBLE"
    cache_layer: Literal["MISS", "SUPPLIER_PROMPT", "LOCAL"] = "MISS"
    duration_ms: int = Field(ge=0)
    retry_count: int = Field(ge=0)


class ManagerConclusion(FrozenModel):
    summary: str
    thesis: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    disagreements: tuple[str, ...] = ()

    @field_validator("summary")
    @classmethod
    def reject_score_language(cls, value: str) -> str:
        lowered = value.lower()
        forbidden = ("total_score", "target_price", "最终评分", "目标价")
        if any(token in lowered for token in forbidden):
            raise ValueError("manager conclusion must not set final score or target price")
        return value


class DataQualityInputs(FrozenModel):
    completeness: Confidence
    freshness: Confidence
    official_source_ratio: Confidence
    cross_source_consistency: Confidence
    schema_validity: Confidence
    evidence_coverage: Confidence
    mean_agent_confidence: Confidence


class CompositeScore(FrozenModel):
    symbol: CanonicalSymbol
    trading_date: date
    decision_at: AwareDatetime
    fundamental_score: Score100
    technical_score: Score100
    sentiment_score: Score100
    quality_confidence_score: Score100
    adjusted_fundamental_score: Score100 = 0
    base_total_score: Score100 = 0
    dividend_bonus: float = Field(ge=0, le=10)
    event_risk_multiplier: float = Field(ge=0, le=1)
    total_score: Score100
    formula_version: str
    agent_bundle_sha256: Sha256
    feature_snapshot_id: UUID
    evidence_bundle_sha256: Sha256

    @model_validator(mode="before")
    @classmethod
    def fill_v1_compatibility_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        result.setdefault("adjusted_fundamental_score", result.get("fundamental_score", 0))
        result.setdefault("base_total_score", result.get("total_score", 0))
        result.setdefault("dividend_bonus", 0.0)
        result.setdefault("event_risk_multiplier", 1.0)
        return result


class Candidate(FrozenModel):
    symbol: CanonicalSymbol
    trading_date: date
    decision_at: AwareDatetime
    total_score: Score100
    base_total_score: Score100 | None = None
    dividend_bonus: float = Field(default=0, ge=0, le=10)
    prediction_percentile: float = Field(ge=0, le=1)
    industry_code: str
    # Defaulted so CandidateArtifact JSON frozen before this field existed still
    # deserializes; new artifacts are not readable by older code.
    industry_name: str | None = None
    volatility: float = Field(gt=0)
    event_risk_multiplier: float = Field(default=1.0, ge=0, le=1)
    style_exposures: dict[str, float] = Field(default_factory=dict)


class PortfolioPosition(FrozenModel):
    symbol: CanonicalSymbol
    industry_code: str
    weight: float = Field(ge=0, le=1)
    target_shares: int = Field(ge=0)
    reference_price: Decimal = Field(gt=0)


class PaperPortfolio(FrozenModel):
    portfolio_id: UUID = Field(default_factory=uuid4)
    trading_date: date
    decision_at: AwareDatetime
    effective_trading_date: date
    status: RunStatus
    positions: tuple[PortfolioPosition, ...]
    expected_turnover: float = Field(ge=0, le=2)
    cash_weight: float = Field(ge=0, le=1)
    constraint_version: str
    input_hash: Sha256

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> PaperPortfolio:
        total = self.cash_weight + sum(position.weight for position in self.positions)
        if abs(total - 1.0) > 1e-8:
            raise ValueError(f"portfolio weights must sum to 1, got {total}")
        return self


class RunManifest(FrozenModel):
    run_id: UUID = Field(default_factory=uuid4)
    run_type: str
    trading_date: date
    decision_at: AwareDatetime
    code_git_sha: str
    config_sha256: Sha256
    dependency_lock_sha256: Sha256
    calendar_version: str
    trade_rule_version: str
    universe_version: str
    feature_set_version: str
    scoring_formula_version: str
    adapter_versions: dict[str, str]
    source_snapshot_ids: tuple[UUID, ...] = ()
    model_artifacts: dict[str, str] = {}
    random_seed: int = 42
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    status: RunStatus = RunStatus.PENDING

    @property
    def manifest_hash(self) -> str:
        return stable_hash(self)


class RawEnvelope(FrozenModel):
    source: str
    dataset: str
    source_record_id: str
    fetched_at: AwareDatetime
    content_type: str
    payload: bytes
    payload_sha256: Sha256
    cursor: str | None = None
    request_metadata: dict[str, Any] = {}

    @model_validator(mode="after")
    def payload_hash_matches(self) -> RawEnvelope:
        from ashare_ai.core.hashing import sha256_bytes

        if sha256_bytes(self.payload) != self.payload_sha256:
            raise ValueError("payload_sha256 does not match payload")
        return self


class SnapshotManifest(FrozenModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    dataset: str
    source: str
    schema_version: str
    adapter_version: str
    fetched_at: AwareDatetime
    row_count: int = Field(ge=0)
    payload_sha256: Sha256
    parquet_uri: str
    status: SnapshotStatus = SnapshotStatus.STAGING
    metadata: dict[str, Any] = {}
