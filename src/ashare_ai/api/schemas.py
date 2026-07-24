from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ashare_ai.core.security import safe_error_text

MAX_WATCHLIST_SYMBOLS = 100
MAX_RESEARCH_SYMBOLS = 100
MAX_TRADE_PLAN_SYMBOLS = 15


def _supported_research_scopes() -> list[Literal["MARKET", "WATCHLIST", "CUSTOM"]]:
    return ["MARKET", "WATCHLIST", "CUSTOM"]


class OrmResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _SanitizedErrorResponse(OrmResponse):
    error_message: str | None

    @field_validator("error_message")
    @classmethod
    def sanitize_error_message(cls, value: str | None) -> str | None:
        return safe_error_text(value) if value else None


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
    password: str = Field(min_length=12, max_length=256)
    role: str = Field(default="USER", pattern=r"^(USER|ADMIN)$")


class UserUpdateRequest(BaseModel):
    enabled: bool | None = None
    role: str | None = Field(default=None, pattern=r"^(USER|ADMIN)$")
    password: str | None = Field(default=None, min_length=12, max_length=256)


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class ModelProfileSettings(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    cache_policy: Literal["GROK", "OPENAI", "COMPATIBLE"] = "COMPATIBLE"
    context_window_tokens: int = Field(default=128000, ge=1024, le=4_000_000)
    output_token_reserve: int = Field(default=8192, ge=0, le=1_000_000)
    reasoning_token_reserve: int = Field(default=0, ge=0, le=1_000_000)
    input_price_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    cached_input_price_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    cache_write_price_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    output_price_per_million: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def reserve_fits_window(self) -> ModelProfileSettings:
        if self.output_token_reserve + self.reasoning_token_reserve >= self.context_window_tokens:
            raise ValueError("output and reasoning reserves must leave input capacity")
        return self


class ModelSettingsRequest(BaseModel):
    base_url: str = Field(min_length=8, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    search_model: str = Field(default="gpt-5.6-luna", min_length=1, max_length=128)
    search_reasoning_effort: str = Field(default="low", pattern=r"^(low|medium|high|xhigh)$")
    research_model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=128)
    research_reasoning_effort: str = Field(default="high", pattern=r"^(low|medium|high|xhigh)$")
    model_profiles: list[ModelProfileSettings] = Field(default_factory=list, max_length=32)
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
    model_profiles: list[ModelProfileSettings] = Field(default_factory=list)
    timeout_seconds: float
    enabled: bool
    configured: bool
    reachable: bool
    degraded: bool
    status_message: str
    checked_at: datetime | None = None
    structured_output_supported: bool = False
    streaming_supported: bool = False


class ModelProbeResponse(BaseModel):
    reachable: bool
    message: str
    model: str
    checked_at: datetime
    structured_output_supported: bool = True
    streaming_supported: bool = False


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
    acquired_on: date | None = None
    profit_trigger_amount: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000000"))
    exit_trigger_price: Decimal | None = Field(default=None, gt=0, le=Decimal("10000000"))
    stop_loss_price: Decimal | None = Field(default=None, gt=0, le=Decimal("10000000"))
    stop_loss_mode: Literal["AUTO_ATR20", "MANUAL", "FALLBACK_8PCT"] = "AUTO_ATR20"
    stop_loss_enabled: bool = True

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def normalize_stop_loss_mode(self) -> PaperPosition:
        if self.stop_loss_price is not None and self.stop_loss_mode != "MANUAL":
            self.stop_loss_mode = "MANUAL"
        if self.stop_loss_price is not None:
            stop_price = Decimal(self.stop_loss_price)
            lower = Decimal(str(self.cost)) * Decimal("0.90")
            upper = Decimal(str(self.cost)) * Decimal("0.95")
            if not lower <= stop_price <= upper:
                raise ValueError("manual stop loss must be 5% to 10% below cost")
        return self


class AssetStateRequest(BaseModel):
    watchlist: list[str] = Field(max_length=MAX_WATCHLIST_SYMBOLS)
    positions: list[PaperPosition] = Field(max_length=15)
    total_assets: float | None = Field(default=None, gt=0, le=1_000_000_000_000)
    exit_monitor_enabled: bool | None = None
    default_profit_trigger: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000000"))
    stop_loss_monitor_enabled: bool | None = None
    buy_monitor_enabled: bool | None = None
    market_refresh_interval_seconds: Literal[15, 30, 60, 120] | None = None

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
    exit_monitor_enabled: bool = False
    default_profit_trigger: Decimal | None = None
    stop_loss_monitor_enabled: bool = True
    buy_monitor_enabled: bool = True
    market_refresh_interval_seconds: Literal[15, 30, 60, 120] = 15
    updated_at: datetime | None = None


class ExitMonitorSettingsRequest(BaseModel):
    """Narrow mobile-safe update that cannot overwrite holdings or the watchlist."""

    exit_monitor_enabled: bool
    default_profit_trigger: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000000"))
    stop_loss_monitor_enabled: bool | None = None
    buy_monitor_enabled: bool | None = None


class MarketRefreshSettingsRequest(BaseModel):
    """Per-account live-market refresh cadence for the interactive Web/App clients."""

    market_refresh_interval_seconds: Literal[15, 30, 60, 120]


class ExitAdviceResponse(_SanitizedErrorResponse):
    advice_id: str
    operation_run_id: str | None = None
    user_id: str
    symbol: str
    status: str
    action: str | None
    decision_at: datetime
    available_at: datetime
    current_price: Decimal
    unrealized_profit: Decimal
    trigger_amount: Decimal
    trigger_type: Literal["PRICE", "PROFIT_AMOUNT", "MANUAL", "STOP_LOSS"] = "PROFIT_AMOUNT"
    trigger_price: Decimal | None = None
    position_snapshot: dict[str, Any]
    research_context: dict[str, Any]
    result: dict[str, Any] | None
    model_name: str | None
    reasoning_effort: str | None
    prompt_version: str
    input_hash: str
    response_sha256: str | None
    cache_hit: bool
    created_at: datetime
    completed_at: datetime | None
    status_url: str | None = None


class ManualExitAdviceRequest(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class NotificationResponse(OrmResponse):
    notification_id: str
    notification_type: str
    severity: Literal["INFO", "WARNING", "HIGH", "CRITICAL"]
    title: str
    body: str
    resource_type: str | None = None
    resource_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime
    expires_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    next_cursor: str | None = None


class NotificationSummaryResponse(BaseModel):
    unread_count: int
    high_risk_unread_count: int
    latest: list[NotificationResponse] = Field(default_factory=list)


class NotificationMarkReadRequest(BaseModel):
    notification_ids: list[str] = Field(min_length=1, max_length=100)


class BuyEntryMonitorResponse(OrmResponse):
    monitor_id: str
    symbol: str
    status: str
    effective_date: date
    expires_at: datetime
    entry_low: Decimal
    entry_high: Decimal
    score_run_id: str | None = None
    trade_plan_id: str | None = None
    rationale: dict[str, Any] = Field(default_factory=dict)
    triggered_at: datetime | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class BuyEntryMonitorRequest(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    enabled: bool = True

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class AIChatThreadRequest(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=128)


class AIChatThreadPatchRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    pinned: bool | None = None
    archived: bool | None = None
    group_label: str | None = Field(default=None, max_length=128)


class AIChatBulkDeleteRequest(BaseModel):
    thread_ids: list[str] = Field(min_length=1, max_length=100)


class AIChatThreadResponse(OrmResponse):
    thread_id: str
    user_id: str
    title: str
    group_mode: Literal["AUTO", "MANUAL"] = "AUTO"
    group_type: Literal["GENERAL", "SINGLE", "MULTI"] = "GENERAL"
    group_label: str | None = None
    cumulative_mentions: list[dict[str, str]] = Field(default_factory=list)
    pinned_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AIChatThreadIndexResponse(BaseModel):
    items: list[AIChatThreadResponse]
    next_cursor: str | None = None


class AIChatMessageResponse(OrmResponse):
    message_id: str
    thread_id: str
    role: Literal["user", "assistant"]
    content: str
    status: Literal["PENDING", "STREAMING", "COMPLETED", "FAILED", "CANCELLED"] = "COMPLETED"
    trading_date: date
    decision_at: datetime
    available_at: datetime
    parent_message_id: str | None = None
    mentioned_symbols: list[str]
    mention_refs: list[dict[str, str]] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)
    model_name: str | None
    reasoning_effort: str | None
    sources: list[dict[str, Any]]
    context_sha256: str | None
    response_sha256: str | None
    cache_hit: bool
    input_tokens: int
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int
    reasoning_tokens: int = 0
    cache_policy: Literal["GROK", "OPENAI", "COMPATIBLE"] = "COMPATIBLE"
    context_budget_status: Literal["WITHIN_BUDGET", "HISTORY_TRIMMED", "CONTEXT_TOO_LARGE"] = (
        "WITHIN_BUDGET"
    )
    error_code: str | None = None
    request_id: str | None = None
    streaming_mode: Literal["STREAMING", "DEGRADED", "CACHED"] = "STREAMING"
    data_status: dict[str, Any] = Field(default_factory=dict)
    response_id: str | None = None
    created_at: datetime


class AIChatMetricResponse(BaseModel):
    metric: Literal["answer", "context", "market", "news", "model"]
    requests: int
    hits: int
    hit_rate: float
    average_latency_ms: float
    average_singleflight_wait_ms: float
    degraded_count: int


class AICostValueResponse(BaseModel):
    requests: int
    cache_hits: int = 0
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    estimated_spend_usd: Decimal
    estimated_savings_usd: Decimal


class AICostBucketResponse(AICostValueResponse):
    bucket_date: date


class AICostTurnResponse(BaseModel):
    requests: int
    cache_hit: bool
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    estimated_spend_usd: Decimal
    estimated_savings_usd: Decimal


class AICostSummaryResponse(BaseModel):
    days: int
    items: list[AICostBucketResponse]
    next_cursor: str | None = None
    totals: AICostValueResponse
    current_turn: AICostTurnResponse | None = None


class SecurityResolveCandidate(BaseModel):
    symbol: str
    name: str


class SecurityResolveResponse(BaseModel):
    query: str
    state: Literal["RESOLVED", "UNRESOLVED", "AMBIGUOUS"]
    candidates: list[SecurityResolveCandidate] = Field(default_factory=list)
    reason_code: str
    decision_at: datetime


class AIChatMentionRef(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=64)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AIChatSendRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    web_search: bool = True
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    mention_refs: list[AIChatMentionRef] = Field(default_factory=list, max_length=5)
    decision_at: datetime | None = None


class AIChatAttachmentResponse(OrmResponse):
    attachment_id: str
    thread_id: str | None
    mime_type: str
    byte_size: int
    width: int
    height: int
    uploaded_at: datetime
    expires_at: datetime
    deleted_at: datetime | None
    deletion_reason: str | None


class PersonalArchiveExportRequest(BaseModel):
    passphrase: str = Field(min_length=8, max_length=128)


class PersonalArchiveApplyRequest(BaseModel):
    merge_options: dict[str, Any] = Field(default_factory=dict)


class PersonalArchiveJobResponse(OrmResponse):
    archive_id: str
    kind: Literal["EXPORT", "IMPORT_PREVIEW", "IMPORT_APPLY"]
    status: Literal["PENDING", "PROCESSING", "SUCCEEDED", "FAILED", "CANCELLED"]
    phase: str
    progress: int = Field(ge=0, le=100)
    source_archive_id: str | None
    result: dict[str, Any] | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime
    deleted_at: datetime | None


class AIModelOptionsResponse(BaseModel):
    models: list[str]
    reasoning_efforts: list[Literal["low", "medium", "high", "xhigh"]]
    web_search_available: bool
    cache_enabled: bool = True


class AppCapabilitiesResponse(BaseModel):
    api_version: Literal["v1"] = "v1"
    authentication: Literal["BEARER_REFRESH"] = "BEARER_REFRESH"
    supported_research_scopes: list[Literal["MARKET", "WATCHLIST", "CUSTOM"]] = Field(
        default_factory=_supported_research_scopes
    )
    max_watchlist_symbols: int = Field(gt=0)
    max_research_symbols: int = Field(gt=0)
    max_trade_plan_symbols: int = Field(gt=0)
    portfolio_target_count: int = Field(gt=0)
    features: dict[str, bool]
    endpoints: dict[str, str]


class AppBootstrapResponse(BaseModel):
    server_time: datetime
    user: UserResponse
    assets: AssetStateResponse
    capabilities: AppCapabilitiesResponse


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
    name: str | None = None
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


class ReportSymbolResponse(BaseModel):
    symbol: str
    name: str | None = None
    research_status: Literal["FORMAL", "FORMAL_WITH_LIMITATIONS", "RISK_BLOCKED"]
    advice_eligible: bool
    recommendation: Literal["NO_BUY"] | None = None
    exclusion_reasons: list[str] = Field(default_factory=list)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    score: ScoreResponse
    rank: int | None = None
    prediction_percentile: float | None = None
    industry_code: str | None = None
    plain_language_summary: str | None = None
    component_summaries: dict[str, str] = Field(default_factory=dict)


class ReportExecutionSymbolStatus(BaseModel):
    symbol: str
    held_quantity: int = Field(ge=0)
    acquired_on: date | None = None
    sellable_quantity: int = Field(ge=0)
    t1_restricted: bool = False
    blockers: list[str] = Field(default_factory=list)


class ReportExecutionStatusResponse(BaseModel):
    report_id: str
    as_of: datetime
    items: list[ReportExecutionSymbolStatus] = Field(default_factory=list)


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


class RunResponse(_SanitizedErrorResponse):
    run_id: str
    run_type: str
    trading_date: date
    decision_at: datetime
    status: str
    input_hash: str
    output_hash: str | None
    started_at: datetime
    completed_at: datetime | None
    # Additive operational metadata for pollers. Existing clients may ignore it.
    data_readiness_state: str | None = None
    next_retry_at: datetime | None = None


class ResearchRequest(BaseModel):
    trading_date: date
    scope: Literal["MARKET", "WATCHLIST", "CUSTOM"] = "MARKET"
    symbols: list[str] = Field(default_factory=list, max_length=MAX_RESEARCH_SYMBOLS)
    total_budget: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000000"))
    per_symbol_budget: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000000"))
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
        if self.scope == "MARKET" and self.symbols:
            raise ValueError("symbols are only accepted for directed research")
        if (
            self.total_budget is not None
            and self.per_symbol_budget is not None
            and self.per_symbol_budget > self.total_budget
        ):
            raise ValueError("per-symbol budget cannot exceed total budget")
        return self


class AutomaticResearchReportSettings(BaseModel):
    slot: Literal["A", "B"]
    enabled: bool = False
    scope: Literal["MARKET", "WATCHLIST", "CUSTOM"] = "MARKET"
    symbols: list[str] = Field(default_factory=list, max_length=MAX_RESEARCH_SYMBOLS)
    total_budget: Decimal = Field(gt=0, le=Decimal("100000000000"))
    per_symbol_budget: Decimal = Field(gt=0, le=Decimal("100000000000"))
    max_stock_price: Decimal | None = Field(default=None, gt=0, le=Decimal("10000000"))
    config_version: int = Field(default=1, ge=1)

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
            raise ValueError("automatic research symbols must be unique")
        return sorted(normalized)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: list[str]) -> list[str]:
        import re

        if any(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", item) is None for item in value):
            raise ValueError("automatic research contains an invalid A-share symbol")
        return value

    @model_validator(mode="after")
    def validate_scope_and_budget(self) -> AutomaticResearchReportSettings:
        if self.scope == "CUSTOM" and not self.symbols:
            raise ValueError("custom automatic research requires at least one symbol")
        if self.scope != "CUSTOM" and self.symbols:
            raise ValueError("symbols are only accepted for custom automatic research")
        if self.per_symbol_budget > self.total_budget:
            raise ValueError("per-symbol budget cannot exceed total budget")
        return self


class ResearchSettingsRequest(BaseModel):
    auto_enabled: bool | None = None
    automatic_reports: list[AutomaticResearchReportSettings] | None = Field(
        default=None, min_length=2, max_length=2
    )

    @model_validator(mode="after")
    def validate_settings_shape(self) -> ResearchSettingsRequest:
        if (self.auto_enabled is None) == (self.automatic_reports is None):
            raise ValueError("submit either auto_enabled or automatic_reports")
        if self.automatic_reports is not None:
            slots = [item.slot for item in self.automatic_reports]
            if set(slots) != {"A", "B"} or len(slots) != len(set(slots)):
                raise ValueError(
                    "automatic_reports must contain report A and report B exactly once"
                )
        return self


class ResearchSettingsResponse(BaseModel):
    auto_enabled: bool = False
    updated_at: datetime | None = None
    automatic_scope: Literal["MARKET", "WATCHLIST", "CUSTOM"] = "MARKET"
    automatic_total_budget: Decimal = Decimal("1000000")
    automatic_per_symbol_budget: Decimal = Decimal("80000")
    automatic_max_stock_price: Decimal | None = None
    automatic_reports: list[AutomaticResearchReportSettings] = Field(default_factory=list)
    schedule_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    schedule_time: Literal["15:05"] = "15:05"
    snapshot_mode: Literal["SYSTEM_ENFORCED"] = "SYSTEM_ENFORCED"
    portfolio_target_count: int = Field(gt=0)


class TradePlanRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=MAX_TRADE_PLAN_SYMBOLS)
    budget_override: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000000"))
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


class TradePlanResponse(_SanitizedErrorResponse):
    plan_id: str
    user_id: str
    report_id: str
    run_id: str
    operation_run_id: str | None = None
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


class RunListResponse(RunResponse):
    user_id: str | None


class RunActivityItem(RunResponse):
    user_id: str | None = None
    resource_type: Literal["RESEARCH", "BACKTEST", "TRADE_PLAN", "EXIT_ADVICE"]
    resource_id: str | None = None
    resource_url: str | None = None
    title: str | None = None
    symbol: str | None = None


class RunActivityResponse(BaseModel):
    items: list[RunActivityItem]
    next_cursor: str | None = None


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
    portfolio_reason_code: str | None = None
    portfolio_reason_message: str | None = None
    trigger_source: Literal["AUTO", "MANUAL"] = "MANUAL"
    automatic_report_slot: Literal["A", "B"] | None = None
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


class BacktestResponse(_SanitizedErrorResponse):
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
    include_quotes: bool = True

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
