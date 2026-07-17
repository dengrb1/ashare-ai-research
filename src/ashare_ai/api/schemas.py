from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrmResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserResponse(OrmResponse):
    user_id: str
    username: str
    role: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=256)
    role: str = Field(default="USER", pattern=r"^(USER|ADMIN)$")


class UserUpdateRequest(BaseModel):
    enabled: bool | None = None
    role: str | None = Field(default=None, pattern=r"^(USER|ADMIN)$")
    password: str | None = Field(default=None, min_length=10, max_length=256)


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=10, max_length=256)


class ScoreResponse(OrmResponse):
    symbol: str
    trading_date: date
    decision_at: datetime
    fundamental_score: float
    technical_score: float
    sentiment_score: float
    quality_confidence_score: float
    total_score: float
    formula_version: str
    agent_bundle_sha256: str
    evidence_bundle_sha256: str
    feature_snapshot_id: str


class CandidateResponse(OrmResponse):
    symbol: str
    trading_date: date
    decision_at: datetime
    rank: int
    total_score: float
    prediction_percentile: float
    industry_code: str
    event_risk_multiplier: float
    style_exposures: dict[str, float]
    evidence_hash: str


class PortfolioResponse(OrmResponse):
    portfolio_id: str
    run_id: str
    trading_date: date
    effective_trading_date: date
    status: str
    expected_turnover: float
    cash_weight: float
    constraint_version: str
    input_hash: str
    positions: list[dict[str, Any]]
    rejection_reasons: list[str]


class RunResponse(OrmResponse):
    run_id: str
    run_type: str
    trading_date: date
    decision_at: datetime
    status: str
    input_hash: str
    output_hash: str | None
    started_at: datetime
    completed_at: datetime | None
    error_message: str | None


class ResearchRequest(BaseModel):
    trading_date: date


class RunListResponse(RunResponse):
    user_id: str | None


class AuditEventResponse(OrmResponse):
    event_id: str
    run_id: str
    event_type: str
    severity: str
    message: str
    details: dict[str, Any]
    created_at: datetime


class BacktestRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    start_date: date
    end_date: date
    snapshot_ids: list[str] = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class BacktestResponse(OrmResponse):
    backtest_id: str
    run_id: str | None
    status: str
    name: str
    start_date: date
    end_date: date
    snapshot_ids: list[str]
    metrics: dict[str, Any] | None
    artifacts: dict[str, Any] | None
    input_hash: str
    output_hash: str | None
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None = None


class SnapshotResponse(OrmResponse):
    snapshot_id: str
    dataset: str
    source: str
    fetched_at: datetime
    row_count: int
    status: str
    details: dict[str, Any]


class ReportBodyResponse(BaseModel):
    report_id: str
    content_type: str
    content: str


class MarketDataStatus(BaseModel):
    source: str
    collected_at: datetime
    cached_at: datetime
    delayed: bool
    stale: bool
    message: str | None = None


class QuoteResponse(BaseModel):
    symbol: str
    name: str | None = None
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: float | None = None
    amount: float | None = None
    status: MarketDataStatus


class KlineBarResponse(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None


class KlineResponse(BaseModel):
    symbol: str
    period: str
    adjustment: str
    bars: list[KlineBarResponse]
    status: MarketDataStatus


class MarketPrefetchRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=500)
    periods: list[str] = Field(default_factory=lambda: ["day"], min_length=1, max_length=5)
    limit: int = Field(default=160, ge=1, le=5000)

    @field_validator("periods")
    @classmethod
    def daily_only(cls, periods: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(period.casefold() for period in periods))
        unsupported = [period for period in normalized if period not in {"day", "daily"}]
        if unsupported:
            raise ValueError(f"unsupported prefetch periods: {unsupported}")
        return ["day"]


class MarketPrefetchResponse(BaseModel):
    quotes: list[QuoteResponse]
    klines: dict[str, dict[str, KlineResponse]]
    errors: dict[str, str]


class ReportResponse(OrmResponse):
    report_id: str
    run_id: str
    trading_date: date
    report_type: str
    object_uri: str
    content_sha256: str
    created_at: datetime
