from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrmResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


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


class ModelSettingsRequest(BaseModel):
    base_url: str = Field(min_length=8, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    search_model: str = Field(default="gpt-5.6-luna", min_length=1, max_length=128)
    search_reasoning_effort: str = Field(default="low", pattern=r"^(low|medium|high|xhigh)$")
    research_model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=128)
    research_reasoning_effort: str = Field(default="high", pattern=r"^(low|medium|high|xhigh)$")
    timeout_seconds: float = Field(default=90, ge=1, le=600)
    enabled: bool = True

    @field_validator("api_key", mode="before")
    @classmethod
    def empty_api_key_keeps_existing_secret(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class ModelSettingsResponse(BaseModel):
    configuration_id: str | None
    version: int
    config_sha256: str
    source: str
    provider: str
    base_url: str
    api_key_configured: bool
    search_model: str
    search_reasoning_effort: str
    research_model: str
    research_reasoning_effort: str
    timeout_seconds: float
    enabled: bool
    configured: bool
    reachable: bool
    degraded: bool
    status_message: str
    checked_at: datetime | None = None


class ModelProbeResponse(BaseModel):
    reachable: bool
    message: str
    model: str
    checked_at: datetime


class ModelListResponse(BaseModel):
    models: list[str]


class PaperPosition(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(default="", max_length=64)
    quantity: int = Field(gt=0, le=1_000_000_000)
    cost: float = Field(gt=0, le=10_000_000)
    # Kept for saved records and older API clients.  Manual holdings now derive
    # their current weight from market value and the account total instead.
    target_weight: float | None = Field(default=None, ge=0, le=1)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AssetStateRequest(BaseModel):
    watchlist: list[str] = Field(max_length=100)
    positions: list[PaperPosition] = Field(max_length=15)
    total_assets: float | None = Field(default=None, gt=0, le=1_000_000_000_000)

    @field_validator("watchlist", mode="before")
    @classmethod
    def normalize_watchlist(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized = [item.strip().upper() if isinstance(item, str) else item for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("watchlist symbols must be unique")
        return normalized

    @field_validator("watchlist")
    @classmethod
    def validate_watchlist_symbols(cls, value: list[str]) -> list[str]:
        import re

        if any(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", item) is None for item in value):
            raise ValueError("watchlist contains an invalid A-share symbol")
        return value

    @field_validator("positions")
    @classmethod
    def validate_positions(cls, value: list[PaperPosition]) -> list[PaperPosition]:
        symbols = [position.symbol for position in value]
        if len(set(symbols)) != len(symbols):
            raise ValueError("position symbols must be unique")
        return value


class AssetStateResponse(BaseModel):
    watchlist: list[str]
    positions: list[PaperPosition]
    total_assets: float | None = None
    updated_at: datetime | None = None


class ScoreResponse(OrmResponse):
    symbol: str
    trading_date: date
    decision_at: datetime
    fundamental_score: float
    technical_score: float
    sentiment_score: float
    quality_confidence_score: float
    base_total_score: float
    dividend_bonus: float
    event_risk_multiplier: float
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
    base_total_score: float | None = None
    dividend_bonus: float = 0
    prediction_percentile: float
    industry_code: str
    event_risk_multiplier: float
    style_exposures: dict[str, float]
    evidence_hash: str


class PortfolioResponse(OrmResponse):
    portfolio_id: str | None = None
    run_id: str
    trading_date: date
    effective_trading_date: date | None = None
    status: str
    expected_turnover: float = 0
    cash_weight: float = 1
    constraint_version: str | None = None
    input_hash: str | None = None
    positions: list[dict[str, Any]] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    observation_only: bool = False
    research_only: bool = False
    message: str | None = None
    reason_code: str | None = None
    formal_eligible_symbols: list[str] = Field(default_factory=list)
    excluded_symbols: dict[str, list[str]] = Field(default_factory=dict)


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
    scope: Literal["MARKET", "WATCHLIST", "CUSTOM"] = "MARKET"
    symbols: list[str] = Field(default_factory=list, max_length=100)
    total_budget: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000000"))
    per_symbol_budget: Decimal | None = Field(
        default=None, gt=0, le=Decimal("100000000000")
    )
    max_stock_price: Decimal | None = Field(default=None, gt=0, le=Decimal("10000000"))

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized = [item.strip().upper() if isinstance(item, str) else item for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("research symbols must be unique")
        return normalized

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: list[str]) -> list[str]:
        import re

        if any(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", item) is None for item in value):
            raise ValueError("research symbols contain an invalid A-share symbol")
        return value

    @model_validator(mode="after")
    def validate_scope_and_budget(self) -> ResearchRequest:
        if self.scope == "CUSTOM" and not self.symbols:
            raise ValueError("custom research requires at least one symbol")
        if self.scope != "CUSTOM" and self.symbols:
            raise ValueError("symbols are only accepted for custom research")
        if (
            self.total_budget is not None
            and self.per_symbol_budget is not None
            and self.per_symbol_budget > self.total_budget
        ):
            raise ValueError("per-symbol budget cannot exceed total budget")
        return self


class ResearchSettingsRequest(BaseModel):
    auto_enabled: bool


class ResearchSettingsResponse(BaseModel):
    auto_enabled: bool = False
    updated_at: datetime | None = None
    automatic_scope: Literal["MARKET"] = "MARKET"
    automatic_total_budget: Decimal = Decimal("1000000")
    automatic_per_symbol_budget: Decimal = Decimal("80000")
    automatic_max_stock_price: Decimal | None = None
    schedule_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    schedule_time: Literal["15:05"] = "15:05"
    snapshot_mode: Literal["SYSTEM_ENFORCED"] = "SYSTEM_ENFORCED"


class TradePlanRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=15)
    budget_override: Decimal | None = Field(
        default=None, gt=0, le=Decimal("100000000000")
    )
    objective: Literal["RISK_ADJUSTED_RETURN"] = "RISK_ADJUSTED_RETURN"

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_trade_plan_symbols(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized = [item.strip().upper() if isinstance(item, str) else item for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("trade plan symbols must be unique")
        return sorted(normalized)

    @field_validator("symbols")
    @classmethod
    def validate_trade_plan_symbols(cls, value: list[str]) -> list[str]:
        import re

        if any(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", item) is None for item in value):
            raise ValueError("trade plan contains an invalid A-share symbol")
        return value


class TradePlanResponse(OrmResponse):
    plan_id: str
    user_id: str
    report_id: str
    run_id: str
    trading_date: date
    decision_at: datetime
    available_at: datetime
    status: str
    objective: str
    symbols: list[str]
    budget_override: Decimal | None
    snapshot_ids: list[str]
    optimizer_version: str
    config_version: str
    prompt_version: str | None
    deterministic_result: dict[str, Any] | None
    ai_explanation: dict[str, Any] | None
    input_hash: str
    output_hash: str | None
    object_uri: str | None
    object_sha256: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None


class RunListResponse(RunResponse):
    user_id: str | None


class ResearchRunResponse(RunResponse):
    phase: str
    progress: int = Field(ge=0, le=100)
    report_id: str | None = None
    report_type: str | None = None
    report_created_at: datetime | None = None
    research_scope: str = "MARKET"
    target_symbols: list[str] = Field(default_factory=list)
    total_budget: Decimal | None = None
    per_symbol_budget: Decimal | None = None
    max_stock_price: Decimal | None = None
    portfolio_requested: bool = True
    portfolio_generated: bool = False
    reason_code: str | None = None
    reason_message: str | None = None
    formal_eligible_count: int | None = None
    excluded_symbol_count: int = 0
    trigger_source: Literal["AUTO", "MANUAL"] = "MANUAL"
    requested_date: date | None = None


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
    retry_count: int = 0
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
    turnover_rate: float | None = None


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
