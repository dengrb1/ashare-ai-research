from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal, NoReturn, cast
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import StreamingResponse

from ashare_ai import __version__
from ashare_ai.agents.attachments import (
    MAX_IMAGES_PER_MESSAGE,
    MAX_MESSAGE_IMAGE_BYTES,
    AttachmentError,
    AttachmentService,
    inspect_image,
)
from ashare_ai.agents.chat import ChatStreamError, allow_chat_request, stream_chat_response
from ashare_ai.agents.chat_context import resolve_security_mentions
from ashare_ai.agents.chat_observability import chat_cost_summary, chat_metric_summary
from ashare_ai.agents.chat_threads import ChatThreadService, InvalidThreadCursor
from ashare_ai.agents.model_settings import (
    ModelConfigurationService,
    ModelProfileDraft,
    ModelSettingsDraft,
    ModelSettingsError,
)
from ashare_ai.api.auth import (
    AuthContext,
    authenticate,
    bootstrap_admin,
    check_auth_rate_limit,
    clear_auth_cookies,
    clear_auth_failures,
    create_session,
    create_token_session,
    hash_password,
    invalidate_user_sessions,
    record_auth_failure,
    require_admin,
    revoke_refresh_token,
    revoke_session,
    rotate_refresh_token,
)
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.api.schemas import (
    MAX_RESEARCH_SYMBOLS,
    MAX_TRADE_PLAN_SYMBOLS,
    MAX_WATCHLIST_SYMBOLS,
    AIChatAttachmentResponse,
    AIChatBulkDeleteRequest,
    AIChatMessageResponse,
    AIChatMetricResponse,
    AIChatSendRequest,
    AIChatThreadIndexResponse,
    AIChatThreadPatchRequest,
    AIChatThreadRequest,
    AIChatThreadResponse,
    AICostSummaryResponse,
    AIModelOptionsResponse,
    AppBootstrapResponse,
    AppCapabilitiesResponse,
    AssetStateRequest,
    AssetStateResponse,
    AuditEventResponse,
    BacktestRequest,
    BacktestResponse,
    BuyEntryMonitorRequest,
    BuyEntryMonitorResponse,
    CandidateResponse,
    ExitAdviceResponse,
    ExitMonitorSettingsRequest,
    HealthResponse,
    KlineResponse,
    LoginRequest,
    ManualExitAdviceRequest,
    MarketPrefetchRequest,
    MarketPrefetchResponse,
    MarketRefreshSettingsRequest,
    MarketSessionStatus,
    ModelListResponse,
    ModelProbeResponse,
    ModelProfileSettings,
    ModelSettingsRequest,
    ModelSettingsResponse,
    NotificationListResponse,
    NotificationMarkReadRequest,
    NotificationResponse,
    NotificationSummaryResponse,
    PasswordResetRequest,
    PersonalArchiveApplyRequest,
    PersonalArchiveExportRequest,
    PersonalArchiveJobResponse,
    PortfolioResponse,
    PushDeliveryReceiptRequest,
    PushDeliveryResponse,
    PushDeviceRequest,
    PushDeviceResponse,
    QuoteResponse,
    RefreshTokenRequest,
    ReportBodyResponse,
    ReportExecutionStatusResponse,
    ReportExecutionSymbolStatus,
    ReportResponse,
    ReportSymbolResponse,
    ResearchRequest,
    ResearchRunResponse,
    ResearchSettingsRequest,
    ResearchSettingsResponse,
    RunActivityItem,
    RunActivityResponse,
    RunListResponse,
    RunResponse,
    ScoreResponse,
    SecurityResolveCandidate,
    SecurityResolveResponse,
    SnapshotResponse,
    SystemResourcesResponse,
    SystemSettingsRequest,
    SystemSettingsResponse,
    SystemSettingsUnlockRequest,
    SystemSettingsUnlockResponse,
    TokenResponse,
    TradePlanRequest,
    TradePlanResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from ashare_ai.api.system_settings_unlock import issue_unlock, require_settings_unlock
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import sha256_bytes, stable_hash
from ashare_ai.core.security import safe_error_message
from ashare_ai.core.system_settings import (
    SECRET_SETTING_FIELDS,
    SystemConfigurationService,
    SystemSettingsError,
    get_effective_settings,
)
from ashare_ai.core.time import SHANGHAI, market_session
from ashare_ai.market.service import get_market_data_service, reset_market_data_service
from ashare_ai.notifications.push import PushConfigurationError, PushDeviceService
from ashare_ai.notifications.service import InvalidNotificationCursor, NotificationService
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.observability.runtime_resources import sample_runtime_resources
from ashare_ai.orchestration.backtest_jobs import enqueue_backtest
from ashare_ai.orchestration.daily import load_pipeline
from ashare_ai.orchestration.exit_advice_jobs import (
    ExitAdviceRequestError,
    create_manual_exit_advice,
    enqueue_exit_advice,
)
from ashare_ai.orchestration.operation_runs import create_operation_run
from ashare_ai.orchestration.personal_archive_jobs import enqueue_personal_archive
from ashare_ai.orchestration.research_jobs import enqueue_research, enqueue_research_at
from ashare_ai.orchestration.research_schedule import (
    AKShareDataReadiness,
    FreeExchangeCalendar,
    data_readiness_wait,
    resolve_manual_research_date,
)
from ashare_ai.orchestration.research_settings import ResearchSettingsService
from ashare_ai.orchestration.trade_plan_queue import (
    PROMPT_VERSION as TRADE_PLAN_PROMPT_VERSION,
)
from ashare_ai.orchestration.trade_plan_queue import (
    enqueue_trade_plan,
)
from ashare_ai.orchestration.worker_status import read_heartbeats
from ashare_ai.portfolio.user_assets import UNSET_TOTAL_ASSETS, UserAssetService
from ashare_ai.reports.chinese_summary import component_summary, symbol_summary
from ashare_ai.search.service import (
    FinancialSearchBusyError,
    FinancialSearchResponse,
    FinancialSearchService,
    FinancialSearchStatus,
    get_financial_search_service,
)
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    AIChatMessage,
    AIChatThread,
    ApiIdempotencyKey,
    AuditEvent,
    BacktestRun,
    BuyEntryMonitorRow,
    CandidateRow,
    ExitAdviceRow,
    JobRun,
    PersonalArchiveJob,
    PortfolioRow,
    ReportRow,
    ScoreRow,
    SecurityMaster,
    SnapshotManifestRow,
    TradePlanRow,
    UserAccount,
    UserAssetState,
)
from ashare_ai.storage.objects import LocalObjectStore, ObjectStore, S3ObjectStore
from ashare_ai.storage.personal_archive import (
    MAX_ARCHIVE_BYTES,
    PersonalArchiveError,
    delete_private_archive,
    private_archive_target_path,
    read_private_archive,
    wrap_job_secret,
)
from ashare_ai.storage.repositories import QueryRepository
from ashare_ai.trading.default_rules import ensure_builtin_trading_rules
from ashare_ai.trading.sellability import position_sellability

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_settings().validate_production_security()
    with SessionLocal() as session:
        bootstrap_admin(session)
        ensure_builtin_trading_rules(session)
        ModelConfigurationService().bootstrap_from_environment(session)
        AttachmentService(session).cleanup_expired()
        session.commit()
    market = get_market_data_service()
    if not market.start():
        logger.warning("AKShare market provider warmup failed; fallbacks remain available")
    try:
        yield
    finally:
        reset_market_data_service()


_api_settings = get_settings()
_production_api = _api_settings.app_env.casefold() == "production"
app = FastAPI(
    title="A-share AI Research API",
    version=__version__,
    description="Authenticated research, live market data and asynchronous fixed-snapshot jobs.",
    lifespan=lifespan,
    docs_url=None if _production_api else "/docs",
    redoc_url=None if _production_api else "/redoc",
    openapi_url=None if _production_api else "/openapi.json",
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_api_settings.trusted_host_list or ["*"])
DbSession = Annotated[Session, Depends(get_db)]
Current = Annotated[AuthContext, Depends(get_auth_context)]
Writer = Annotated[AuthContext, Depends(get_write_context)]
IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]
SystemSettingsUnlockToken = Annotated[str | None, Header(alias="X-System-Settings-Unlock")]

_market_session_calendar_cache: dict[date, tuple[date, ...]] = {}
_market_session_calendar_cache_lock = Lock()


def _market_session_status(now: datetime | None = None) -> MarketSessionStatus:
    """Return a fail-closed live-session status without exposing calendar internals."""

    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    trading_date = current.date()
    with _market_session_calendar_cache_lock:
        sessions = _market_session_calendar_cache.get(trading_date)
    if sessions is None:
        try:
            sessions = FreeExchangeCalendar().sessions(trading_date, trading_date)
        except Exception:
            domain_status = market_session(current, None)
        else:
            with _market_session_calendar_cache_lock:
                _market_session_calendar_cache[trading_date] = sessions
            domain_status = market_session(current, sessions)
    else:
        domain_status = market_session(current, sessions)
    return MarketSessionStatus(
        state=domain_status.state,
        as_of=current,
        trading_date=trading_date,
        is_trading_day=domain_status.is_trading_day,
        reason=domain_status.reason,
    )


def _idempotency_fingerprint(
    user_id: str, route: str, key: str | None, payload: Any
) -> tuple[str, str] | None:
    if key is None:
        return None
    return (
        stable_hash({"user_id": user_id, "route": route, "key": key}),
        stable_hash(payload),
    )


def _find_idempotency(
    db: Session,
    *,
    user_id: str,
    route: str,
    fingerprint: tuple[str, str] | None,
) -> ApiIdempotencyKey | None:
    if fingerprint is None:
        return None
    key_sha256, request_sha256 = fingerprint
    row = db.scalar(
        select(ApiIdempotencyKey).where(
            ApiIdempotencyKey.user_id == user_id,
            ApiIdempotencyKey.route == route,
            ApiIdempotencyKey.key_sha256 == key_sha256,
        )
    )
    if row is not None and row.request_sha256 != request_sha256:
        raise HTTPException(
            status_code=409, detail="Idempotency-Key was reused with another request"
        )
    return row


def _remember_idempotency(
    db: Session,
    *,
    user_id: str,
    route: str,
    fingerprint: tuple[str, str] | None,
    resource_type: str,
    resource_id: str,
) -> None:
    if fingerprint is None:
        return
    key_sha256, request_sha256 = fingerprint
    db.add(
        ApiIdempotencyKey(
            user_id=user_id,
            route=route,
            key_sha256=key_sha256,
            request_sha256=request_sha256,
            resource_type=resource_type,
            resource_id=resource_id,
            created_at=datetime.now(UTC),
        )
    )


SearchService = Annotated[FinancialSearchService, Depends(get_financial_search_service)]


@app.middleware("http")
async def api_security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.url.path.startswith("/api/v1/"):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )
    return response


def _admin(context: AuthContext) -> None:
    require_admin(context)


def _owns(run: JobRun, context: AuthContext) -> bool:
    return context.user.role == "ADMIN" or run.user_id == context.user.user_id


def _result_access(context: AuthContext) -> dict[str, Any]:
    return {
        "user_id": context.user.user_id,
        "include_all_users": context.user.role == "ADMIN",
    }


def _security_names_at(db: Session, symbols: set[str], decision_at: datetime) -> dict[str, str]:
    """Return the latest point-in-time short name for every requested symbol."""

    if not symbols:
        return {}
    rows = db.scalars(
        select(SecurityMaster)
        .where(
            SecurityMaster.symbol.in_(symbols),
            SecurityMaster.available_at <= decision_at,
            SecurityMaster.effective_from <= decision_at.astimezone(SHANGHAI).date(),
            or_(
                SecurityMaster.effective_to.is_(None),
                SecurityMaster.effective_to >= decision_at.astimezone(SHANGHAI).date(),
            ),
        )
        .order_by(
            SecurityMaster.symbol,
            SecurityMaster.available_at.desc(),
            SecurityMaster.fetched_at.desc(),
        )
    ).all()
    names: dict[str, str] = {}
    for row in rows:
        names.setdefault(row.symbol, row.short_name)
    return names


def _manual_research_date(requested_date: date, now: datetime) -> date:
    current = now.astimezone(SHANGHAI) if now.tzinfo is not None else now.replace(tzinfo=SHANGHAI)
    if requested_date > current.date():
        raise HTTPException(
            status_code=422, detail="research requested_date cannot be in the future"
        )
    if get_effective_settings().canonical_bundle_mode in {"file", "demo"}:
        return requested_date
    try:
        sessions = FreeExchangeCalendar().sessions(
            requested_date - timedelta(days=20), current.date() + timedelta(days=10)
        )
        return resolve_manual_research_date(
            requested_date=requested_date,
            now=current,
            sessions=sessions,
            data_ready=lambda _: True,
            require_ready=False,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=safe_error_message(exc),
        ) from exc


def _research_readiness_wait(trading_date: date, now: datetime) -> dict[str, str | int] | None:
    """Return durable wait metadata without changing the selected session."""
    if get_effective_settings().canonical_bundle_mode in {"file", "demo"}:
        return None
    current = now.astimezone(SHANGHAI) if now.tzinfo else now.replace(tzinfo=SHANGHAI)
    try:
        ready = AKShareDataReadiness().ready(trading_date, current)
    except Exception:
        ready = False
    if ready:
        return None
    sessions = FreeExchangeCalendar().sessions(trading_date, trading_date + timedelta(days=14))
    return data_readiness_wait(
        trading_date=trading_date,
        now=current,
        sessions=sessions,
        retry_minutes=get_effective_settings().daily_research_retry_minutes,
    )


def _trade_plan_policy_payload(run: JobRun) -> dict[str, Any]:
    manifest = run.manifest if isinstance(run.manifest, dict) else {}
    configured_path = manifest.get("policy_config_path")
    path = Path(str(configured_path)) if configured_path else get_settings().policy_config_path
    if not path.is_file():
        raise HTTPException(status_code=409, detail="pinned policy configuration is unavailable")
    payload = path.read_bytes()
    expected_hash = manifest.get("policy_config_sha256", manifest.get("policy_sha256"))
    if isinstance(expected_hash, str) and expected_hash and sha256_bytes(payload) != expected_hash:
        raise HTTPException(status_code=409, detail="pinned policy configuration hash mismatch")
    parsed = json.loads(payload)
    policy = parsed.get("trade_plan") if isinstance(parsed, dict) else None
    if isinstance(policy, dict):
        return policy
    return {
        "history_sessions": 240,
        "training_sessions": 160,
        "validation_sessions": 80,
        "entry_step_sessions": 10,
        "minimum_completed_trades": 5,
        "maximum_drawdown": 0.12,
        "entry_discounts": [0, 0.01, 0.02],
        "take_profits": [0.08, 0.12, 0.16],
        "stop_losses": [0.05, 0.08, 0.10],
        "trailing_stops": [0.05, 0.08],
        "maximum_holding_sessions": [10, 20, 40, 60],
        "entry_valid_sessions": 3,
        "score_exit_threshold": 60,
    }


def _configured_portfolio_target_count() -> int:
    path = get_settings().policy_config_path
    try:
        parsed = json.loads(path.read_bytes())
        value = parsed["portfolio"]["target_count"]
        target_count = int(value)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=503, detail="versioned portfolio target_count is unavailable"
        ) from exc
    if target_count <= 0:
        raise HTTPException(status_code=503, detail="portfolio target_count must be positive")
    return target_count


def _trade_plan_error(code: str, message: str, *, symbols: list[str] | None = None) -> NoReturn:
    detail: dict[str, Any] = {"code": code, "message": message}
    if symbols:
        detail["symbols"] = symbols
    raise HTTPException(status_code=409, detail=detail)


def _backtest_response(db: Session, row: BacktestRun) -> BacktestResponse:
    job = db.get(JobRun, row.run_id) if row.run_id else None
    return BacktestResponse.model_validate(
        {
            **{column.name: getattr(row, column.name) for column in row.__table__.columns},
            "error_message": job.error_message if job else None,
        }
    )


_RESEARCH_PHASES = {
    "sync_reference_data": ("同步参考数据", 10),
    "ingest_and_verify": ("冻结并校验快照", 22),
    "build_universe": ("构建可交易池", 34),
    "build_features": ("计算研究特征", 46),
    "run_research_agents": ("执行结构化研究", 60),
    "calculate_scores": ("生成确定性评分", 72),
    "qlib_filter": ("筛选候选池", 82),
    "risk_state": ("评估组合风控", 88),
    "build_portfolio": ("生成模拟组合", 94),
    "publish_report": ("发布研究报告", 98),
}


def read_backtest_bundle(snapshot_uris: dict[str, str], expected_hashes: dict[str, str]) -> Any:
    """Load the Parquet validator only for retry requests, not for every API process."""

    from ashare_ai.orchestration.builtin_backtest import read_backtest_bundle as load_bundle

    return load_bundle(snapshot_uris, expected_hashes)


def _research_run_response(db: Session, row: JobRun) -> ResearchRunResponse:
    report = db.scalar(
        select(ReportRow)
        .where(ReportRow.run_id == row.run_id)
        .order_by(ReportRow.created_at.desc())
        .limit(1)
    )
    normalized = row.status.upper()
    if normalized in {"SUCCEEDED", "FUSED"}:
        phase, progress = (
            ("观察模式报告已发布", 100) if normalized == "FUSED" else ("研究结果已发布", 100)
        )
    elif normalized in {"FAILED", "CANCEL_REQUESTED", "CANCELLED"}:
        latest = db.scalar(
            select(AuditEvent)
            .where(AuditEvent.run_id == row.run_id, AuditEvent.event_type == "STAGE_COMPLETED")
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )
        stage = str(latest.details.get("stage", "")) if latest is not None else ""
        _, progress = _RESEARCH_PHASES.get(stage, ("", 5))
        phase = {
            "FAILED": "研究失败",
            "CANCEL_REQUESTED": "正在停止（当前阶段完成后）",
            "CANCELLED": "研究已停止",
        }[normalized]
    elif normalized == "DATA_READINESS_WAITING":
        phase, progress = "等待基准数据同步", 0
    elif normalized in {"PENDING", "QUEUED"}:
        phase, progress = "等待 Worker", 0
    else:
        latest = db.scalar(
            select(AuditEvent)
            .where(AuditEvent.run_id == row.run_id, AuditEvent.event_type == "STAGE_COMPLETED")
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )
        stage = str(latest.details.get("stage", "")) if latest is not None else ""
        phase, progress = _RESEARCH_PHASES.get(stage, ("启动研究流水线", 5))
    manifest = dict(row.manifest or {})
    budget = manifest.get("research_budget")
    budget = budget if isinstance(budget, dict) else {}
    gate = manifest.get("data_quality_gate")
    gate = gate if isinstance(gate, dict) else {}
    risk_outcome = manifest.get("risk_outcome")
    risk_outcome = risk_outcome if isinstance(risk_outcome, dict) else {}
    portfolio_outcome = manifest.get("portfolio_outcome")
    portfolio_outcome = portfolio_outcome if isinstance(portfolio_outcome, dict) else {}
    portfolio_generated = (
        db.scalar(
            select(PortfolioRow.portfolio_id).where(PortfolioRow.run_id == row.run_id).limit(1)
        )
        is not None
    )
    return ResearchRunResponse.model_validate(
        {
            **{column.name: getattr(row, column.name) for column in row.__table__.columns},
            "phase": phase,
            "progress": progress,
            "report_id": report.report_id if report else None,
            "report_type": report.report_type if report else None,
            "report_created_at": report.created_at if report else None,
            "research_scope": manifest.get("research_scope", "MARKET"),
            "target_symbols": manifest.get("target_symbols", []),
            "total_budget": budget.get("total_budget"),
            "per_symbol_budget": budget.get("per_symbol_budget"),
            "max_stock_price": budget.get("max_stock_price"),
            "portfolio_requested": bool(manifest.get("portfolio_requested", True)),
            "portfolio_generated": portfolio_generated,
            "reason_code": risk_outcome.get("reason_code"),
            "reason_message": risk_outcome.get("reason_message"),
            "formal_eligible_count": gate.get("formal_eligible_count"),
            "excluded_symbol_count": len(gate.get("excluded_symbols", {})),
            "portfolio_reason_code": portfolio_outcome.get("reason_code"),
            "portfolio_reason_message": portfolio_outcome.get("reason_message"),
            "trigger_source": manifest.get("trigger_source", "MANUAL"),
            "automatic_report_slot": manifest.get("automatic_report_slot"),
            "requested_date": manifest.get("requested_date", row.trading_date),
            "data_readiness_state": (
                "WAITING_FOR_BENCHMARKS" if normalized == "DATA_READINESS_WAITING" else None
            ),
            "next_retry_at": (manifest.get("data_readiness_wait") or {}).get("next_retry_at"),
        }
    )


@app.get("/api/v1/health", response_model=HealthResponse)
def health(db: DbSession) -> HealthResponse:
    database = "ok"
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        database = "unavailable"
    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        database=database,
    )


@app.post("/api/v1/auth/login", response_model=UserResponse)
def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> UserResponse:
    bootstrap_admin(db)
    source = check_auth_rate_limit(request, payload.username)
    user = authenticate(db, payload.username, payload.password)
    if user is None:
        record_auth_failure(source)
        raise HTTPException(status_code=401, detail="invalid username or password")
    clear_auth_failures(source)
    create_session(db, user, response, request)
    return UserResponse.model_validate(user)


@app.post("/api/v1/auth/token", response_model=TokenResponse)
def issue_token(payload: LoginRequest, request: Request, db: DbSession) -> TokenResponse:
    bootstrap_admin(db)
    source = check_auth_rate_limit(request, payload.username)
    user = authenticate(db, payload.username, payload.password)
    if user is None:
        record_auth_failure(source)
        raise HTTPException(status_code=401, detail="invalid username or password")
    clear_auth_failures(source)
    pair = create_token_session(db, user, request)
    return TokenResponse(
        access_token=pair.access_token,
        expires_in=pair.access_expires_in,
        refresh_token=pair.refresh_token,
        refresh_expires_in=pair.refresh_expires_in,
    )


@app.post("/api/v1/auth/refresh", response_model=TokenResponse)
def refresh_token(payload: RefreshTokenRequest, db: DbSession) -> TokenResponse:
    pair = rotate_refresh_token(db, payload.refresh_token)
    return TokenResponse(
        access_token=pair.access_token,
        expires_in=pair.access_expires_in,
        refresh_token=pair.refresh_token,
        refresh_expires_in=pair.refresh_expires_in,
    )


@app.post("/api/v1/auth/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(payload: RefreshTokenRequest, db: DbSession) -> None:
    revoke_refresh_token(db, payload.refresh_token)


@app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, db: DbSession, context: Writer) -> None:
    revoke_session(db, context)
    clear_auth_cookies(response)


@app.get("/api/v1/auth/me", response_model=UserResponse)
def me(context: Current) -> UserResponse:
    return UserResponse.model_validate(context.user)


@app.get("/api/v1/assets", response_model=AssetStateResponse)
def asset_state(db: DbSession, context: Current) -> AssetStateResponse:
    return AssetStateResponse.model_validate(UserAssetService(db).get(context.user.user_id))


@app.get("/api/v1/app/bootstrap", response_model=AppBootstrapResponse)
def app_bootstrap(db: DbSession, context: Current) -> AppBootstrapResponse:
    """Return the stable, authenticated initialization contract for native clients."""
    assets = AssetStateResponse.model_validate(UserAssetService(db).get(context.user.user_id))
    return AppBootstrapResponse(
        server_time=datetime.now(UTC),
        user=UserResponse.model_validate(context.user),
        assets=assets,
        capabilities=AppCapabilitiesResponse(
            max_watchlist_symbols=MAX_WATCHLIST_SYMBOLS,
            max_research_symbols=MAX_RESEARCH_SYMBOLS,
            max_trade_plan_symbols=MAX_TRADE_PLAN_SYMBOLS,
            portfolio_target_count=_configured_portfolio_target_count(),
            features={
                "watchlist_research_selection": True,
                "formal_watchlist_reports": True,
                "report_symbol_eligibility": True,
                "trade_plan_generation": True,
                "research_cancellation": True,
                "idempotency_key": True,
                "paper_portfolio_only": True,
                "profit_exit_monitor": True,
                "stop_loss_monitor": True,
                "buy_entry_monitor": True,
                "market_refresh_interval_setting": True,
                "notifications": True,
                "chat_context_metrics": True,
                "ai_cost_summary": True,
                "persistent_ai_chat": True,
                "chat_images_seven_day_retention": True,
                "personal_archive_export_import": True,
                "searxng_web_research": True,
            },
            endpoints={
                "assets": "/api/v1/assets",
                "exit_monitor_settings": "/api/v1/assets/exit-monitor",
                "market_refresh_settings": "/api/v1/assets/market-refresh",
                "research_runs": "/api/v1/research/runs",
                "research_run": "/api/v1/research/runs/{run_id}",
                "research_settings": "/api/v1/research/settings",
                "report_symbols": "/api/v1/reports/{report_id}/symbols",
                "report_execution_status": "/api/v1/reports/{report_id}/execution-status",
                "report_trade_plans": "/api/v1/reports/{report_id}/trade-plans",
                "run_activity": "/api/v1/runs/activity",
                "trade_plan": "/api/v1/trade-plans/{plan_id}",
                "exit_advice": "/api/v1/exit-advice",
                "manual_exit_advice": "/api/v1/exit-advice/manual",
                "buy_entry_monitors": "/api/v1/buy-entry-monitors",
                "notifications": "/api/v1/notifications",
                "notification_summary": "/api/v1/notifications/summary",
                "security_resolve": "/api/v1/securities/resolve",
                "chat_metrics": "/api/v1/ai/chat/metrics",
                "ai_costs": "/api/v1/ai/costs",
                "ai_chat_threads": "/api/v1/ai/chat/threads",
                "ai_chat_thread_index": "/api/v1/ai/chat/thread-index",
                "personal_data_exports": "/api/v1/me/data-exports",
                "personal_data_imports": "/api/v1/me/data-imports",
            },
        ),
    )


@app.put("/api/v1/assets", response_model=AssetStateResponse)
def update_asset_state(
    payload: AssetStateRequest, db: DbSession, context: Writer
) -> AssetStateResponse:
    service = UserAssetService(db)
    previous = service.get(context.user.user_id)
    state = service.save(
        context.user.user_id,
        payload.watchlist,
        [position.model_dump(mode="json", exclude_none=True) for position in payload.positions],
        payload.total_assets if "total_assets" in payload.model_fields_set else UNSET_TOTAL_ASSETS,
        (
            bool(payload.exit_monitor_enabled)
            if "exit_monitor_enabled" in payload.model_fields_set
            else bool(previous.get("exit_monitor_enabled", False))
        ),
        (
            float(payload.default_profit_trigger)
            if "default_profit_trigger" in payload.model_fields_set
            and payload.default_profit_trigger is not None
            else (
                None
                if "default_profit_trigger" in payload.model_fields_set
                else previous.get("default_profit_trigger")
            )
        ),
        (
            bool(payload.stop_loss_monitor_enabled)
            if "stop_loss_monitor_enabled" in payload.model_fields_set
            else bool(previous.get("stop_loss_monitor_enabled", True))
        ),
        (
            bool(payload.buy_monitor_enabled)
            if "buy_monitor_enabled" in payload.model_fields_set
            else bool(previous.get("buy_monitor_enabled", True))
        ),
        (
            int(payload.market_refresh_interval_seconds)
            if "market_refresh_interval_seconds" in payload.model_fields_set
            and payload.market_refresh_interval_seconds is not None
            else None
        ),
    )
    return AssetStateResponse.model_validate(state)


@app.put("/api/v1/assets/exit-monitor", response_model=AssetStateResponse)
def update_exit_monitor_settings(
    payload: ExitMonitorSettingsRequest,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> AssetStateResponse:
    """Update only exit-monitor fields so native clients cannot overwrite stale asset data."""

    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    route = "/api/v1/assets/exit-monitor"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id, route, idempotency_key, payload.model_dump(mode="json")
    )
    replay = _find_idempotency(
        db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
    )
    service = UserAssetService(db)
    if replay is not None:
        if replay.resource_type != "EXIT_MONITOR_SETTINGS":
            raise HTTPException(status_code=409, detail="idempotent resource is unavailable")
        return AssetStateResponse.model_validate(service.get(context.user.user_id))
    previous = service.get(context.user.user_id)
    state = service.save(
        context.user.user_id,
        previous["watchlist"],
        previous["positions"],
        UNSET_TOTAL_ASSETS,
        payload.exit_monitor_enabled,
        (
            float(payload.default_profit_trigger)
            if payload.default_profit_trigger is not None
            else None
        ),
        (
            payload.stop_loss_monitor_enabled
            if payload.stop_loss_monitor_enabled is not None
            else bool(previous.get("stop_loss_monitor_enabled", True))
        ),
        (
            payload.buy_monitor_enabled
            if payload.buy_monitor_enabled is not None
            else bool(previous.get("buy_monitor_enabled", True))
        ),
        commit=False,
    )
    _remember_idempotency(
        db,
        user_id=context.user.user_id,
        route=route,
        fingerprint=fingerprint,
        resource_type="EXIT_MONITOR_SETTINGS",
        resource_id=context.user.user_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        winner = _find_idempotency(
            db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
        )
        if winner is not None and winner.resource_type == "EXIT_MONITOR_SETTINGS":
            return AssetStateResponse.model_validate(service.get(context.user.user_id))
        raise HTTPException(status_code=409, detail="exit monitor update conflicted") from exc
    return AssetStateResponse.model_validate(state)


@app.put("/api/v1/assets/market-refresh", response_model=AssetStateResponse)
def update_market_refresh_settings(
    payload: MarketRefreshSettingsRequest,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> AssetStateResponse:
    """Persist only live-market polling settings, without replacing asset records."""

    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    route = "/api/v1/assets/market-refresh"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id, route, idempotency_key, payload.model_dump(mode="json")
    )
    replay = _find_idempotency(
        db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
    )
    service = UserAssetService(db)
    if replay is not None:
        if replay.resource_type != "MARKET_REFRESH_SETTINGS":
            raise HTTPException(status_code=409, detail="idempotent resource is unavailable")
        return AssetStateResponse.model_validate(service.get(context.user.user_id))
    previous = service.get(context.user.user_id)
    state = service.save(
        context.user.user_id,
        previous["watchlist"],
        previous["positions"],
        UNSET_TOTAL_ASSETS,
        bool(previous.get("exit_monitor_enabled", False)),
        previous.get("default_profit_trigger"),
        bool(previous.get("stop_loss_monitor_enabled", True)),
        bool(previous.get("buy_monitor_enabled", True)),
        int(payload.market_refresh_interval_seconds),
        commit=False,
    )
    _remember_idempotency(
        db,
        user_id=context.user.user_id,
        route=route,
        fingerprint=fingerprint,
        resource_type="MARKET_REFRESH_SETTINGS",
        resource_id=context.user.user_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        winner = _find_idempotency(
            db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
        )
        if winner is not None and winner.resource_type == "MARKET_REFRESH_SETTINGS":
            return AssetStateResponse.model_validate(service.get(context.user.user_id))
        raise HTTPException(status_code=409, detail="market refresh update conflicted") from exc
    return AssetStateResponse.model_validate(state)


@app.post(
    "/api/v1/exit-advice/manual",
    response_model=ExitAdviceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_manual_exit_advice(
    payload: ManualExitAdviceRequest,
    response: Response,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> ExitAdviceResponse:
    """Queue a paper-only exit study for one of the caller's current positions."""

    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    route = "/api/v1/exit-advice/manual"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id, route, idempotency_key, payload.model_dump(mode="json")
    )
    replay = _find_idempotency(
        db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
    )
    if replay is not None:
        row = db.get(ExitAdviceRow, replay.resource_id)
        if (
            replay.resource_type != "EXIT_ADVICE"
            or row is None
            or row.user_id != context.user.user_id
        ):
            raise HTTPException(status_code=409, detail="idempotent resource is unavailable")
        response.status_code = status.HTTP_200_OK
        return ExitAdviceResponse.model_validate(row).model_copy(
            update={"status_url": f"/api/v1/exit-advice/{row.advice_id}"}
        )
    try:
        row = create_manual_exit_advice(
            db,
            user_id=context.user.user_id,
            symbol=payload.symbol,
        )
    except ExitAdviceRequestError as exc:
        # Codes are stable client-facing states; provider errors and position data
        # are deliberately not reflected in this public response.
        raise HTTPException(status_code=422, detail=exc.code) from exc
    if row.operation_run_id is None:
        operation = create_operation_run(
            db,
            user_id=row.user_id,
            run_type="EXIT_ADVICE",
            resource_id=row.advice_id,
            trading_date=row.decision_at.astimezone(SHANGHAI).date(),
            decision_at=row.decision_at,
            input_hash=row.input_hash,
            manifest={"symbol": row.symbol, "trigger_type": row.trigger_type},
            created_at=row.created_at,
        )
        row.operation_run_id = operation.run_id
    _remember_idempotency(
        db,
        user_id=context.user.user_id,
        route=route,
        fingerprint=fingerprint,
        resource_type="EXIT_ADVICE",
        resource_id=row.advice_id,
    )
    try:
        db.commit()
        enqueue_exit_advice(row.advice_id)
    except IntegrityError as exc:
        db.rollback()
        replay = _find_idempotency(
            db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
        )
        if replay is not None:
            winner = db.get(ExitAdviceRow, replay.resource_id)
            if winner is not None and winner.user_id == context.user.user_id:
                response.status_code = status.HTTP_200_OK
                return ExitAdviceResponse.model_validate(winner).model_copy(
                    update={"status_url": f"/api/v1/exit-advice/{winner.advice_id}"}
                )
        raise HTTPException(status_code=409, detail="exit advice submission conflicted") from exc
    except Exception as exc:
        db.rollback()
        failed = db.get(ExitAdviceRow, row.advice_id)
        if failed is not None:
            failed.status = "FAILED"
            failed.error_message = "QUEUE_UNAVAILABLE"
            failed.completed_at = datetime.now(UTC)
            db.commit()
        raise HTTPException(status_code=503, detail="exit advice queue unavailable") from exc
    return ExitAdviceResponse.model_validate(row).model_copy(
        update={"status_url": f"/api/v1/exit-advice/{row.advice_id}"}
    )


@app.get("/api/v1/exit-advice", response_model=list[ExitAdviceResponse])
def exit_advice_list(
    db: DbSession,
    context: Current,
    limit: int = Query(default=20, ge=1, le=100),
    before: datetime | None = None,
) -> list[ExitAdviceResponse]:
    statement = select(ExitAdviceRow).where(ExitAdviceRow.user_id == context.user.user_id)
    if before is not None:
        statement = statement.where(ExitAdviceRow.created_at < before)
    rows = db.scalars(statement.order_by(ExitAdviceRow.created_at.desc()).limit(limit)).all()
    return [ExitAdviceResponse.model_validate(row) for row in rows]


@app.get("/api/v1/exit-advice/{advice_id}", response_model=ExitAdviceResponse)
def exit_advice_detail(advice_id: str, db: DbSession, context: Current) -> ExitAdviceResponse:
    row = db.scalar(
        select(ExitAdviceRow).where(
            ExitAdviceRow.advice_id == advice_id,
            ExitAdviceRow.user_id == context.user.user_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="exit advice not found")
    return ExitAdviceResponse.model_validate(row)


@app.get("/api/v1/buy-entry-monitors", response_model=list[BuyEntryMonitorResponse])
def buy_entry_monitor_list(
    db: DbSession,
    context: Current,
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = None,
) -> list[BuyEntryMonitorResponse]:
    statement = select(BuyEntryMonitorRow).where(BuyEntryMonitorRow.user_id == context.user.user_id)
    if before is not None:
        statement = statement.where(BuyEntryMonitorRow.updated_at < before)
    rows = db.scalars(
        statement.order_by(
            BuyEntryMonitorRow.updated_at.desc(),
            BuyEntryMonitorRow.monitor_id.desc(),
        ).limit(limit)
    ).all()
    return [BuyEntryMonitorResponse.model_validate(row) for row in rows]


@app.put("/api/v1/buy-entry-monitors", response_model=list[BuyEntryMonitorResponse])
def update_buy_entry_monitor(
    payload: BuyEntryMonitorRequest,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> list[BuyEntryMonitorResponse]:
    """Enable or cancel a watchlist symbol's derived paper-entry monitors."""

    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    assets = UserAssetService(db).get(context.user.user_id)
    if payload.enabled and payload.symbol not in set(assets["watchlist"]):
        raise HTTPException(status_code=422, detail="SYMBOL_NOT_IN_WATCHLIST")
    route = "/api/v1/buy-entry-monitors"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id, route, idempotency_key, payload.model_dump(mode="json")
    )
    replay = _find_idempotency(
        db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
    )
    if replay is None:
        now = datetime.now(UTC)
        rows = list(
            db.scalars(
                select(BuyEntryMonitorRow).where(
                    BuyEntryMonitorRow.user_id == context.user.user_id,
                    BuyEntryMonitorRow.symbol == payload.symbol,
                    BuyEntryMonitorRow.status == "ACTIVE",
                )
            ).all()
        )
        if not payload.enabled:
            for row in rows:
                row.status = "CANCELLED"
                row.updated_at = now
        _remember_idempotency(
            db,
            user_id=context.user.user_id,
            route=route,
            fingerprint=fingerprint,
            resource_type="BUY_ENTRY_MONITOR_SETTING",
            resource_id=payload.symbol,
        )
        db.commit()
    return [
        BuyEntryMonitorResponse.model_validate(row)
        for row in db.scalars(
            select(BuyEntryMonitorRow)
            .where(
                BuyEntryMonitorRow.user_id == context.user.user_id,
                BuyEntryMonitorRow.symbol == payload.symbol,
            )
            .order_by(BuyEntryMonitorRow.updated_at.desc())
        ).all()
    ]


@app.get("/api/v1/notifications", response_model=NotificationListResponse)
def notification_list(
    db: DbSession,
    context: Current,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    unread_only: bool = False,
) -> NotificationListResponse:
    try:
        items, next_cursor = NotificationService(db).list(
            context.user.user_id,
            limit=limit,
            cursor=cursor,
            unread_only=unread_only,
        )
    except InvalidNotificationCursor as exc:
        raise HTTPException(status_code=422, detail="invalid notification cursor") from exc
    return NotificationListResponse(
        items=[NotificationResponse.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@app.get("/api/v1/notifications/summary", response_model=NotificationSummaryResponse)
def notification_summary(db: DbSession, context: Current) -> NotificationSummaryResponse:
    summary = NotificationService(db).summary(context.user.user_id)
    return NotificationSummaryResponse(
        unread_count=summary["unread_count"],
        high_risk_unread_count=summary["high_risk_unread_count"],
        latest=[NotificationResponse.model_validate(item) for item in summary["latest"]],
    )


@app.get("/api/v1/notifications/{notification_id}", response_model=NotificationResponse)
def notification_detail(
    notification_id: str,
    db: DbSession,
    context: Current,
) -> NotificationResponse:
    from ashare_ai.storage.models import NotificationRow

    row = db.scalar(
        select(NotificationRow).where(
            NotificationRow.notification_id == notification_id,
            NotificationRow.user_id == context.user.user_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="notification not found")
    return NotificationResponse.model_validate(row)


@app.post("/api/v1/devices", response_model=PushDeviceResponse)
def register_push_device(
    payload: PushDeviceRequest,
    db: DbSession,
    context: Writer,
) -> PushDeviceResponse:
    try:
        row = PushDeviceService(db).register(
            user_id=context.user.user_id,
            installation_id=payload.installation_id,
            registration_id=payload.registration_id,
            app_version=payload.app_version,
            os_version=payload.os_version,
            device_model=payload.device_model,
        )
    except PushConfigurationError as exc:
        raise HTTPException(status_code=503, detail="push registration is unavailable") from exc
    db.commit()
    return PushDeviceResponse.model_validate(row)


@app.delete("/api/v1/devices/{device_id}", status_code=204)
def unregister_push_device(device_id: str, db: DbSession, context: Writer) -> Response:
    if not PushDeviceService(db).disable(user_id=context.user.user_id, device_id=device_id):
        raise HTTPException(status_code=404, detail="device not found")
    db.commit()
    return Response(status_code=204)


@app.post(
    "/api/v1/devices/{device_id}/deliveries",
    response_model=PushDeliveryResponse,
)
def acknowledge_push_delivery(
    device_id: str,
    payload: PushDeliveryReceiptRequest,
    db: DbSession,
    context: Writer,
) -> PushDeliveryResponse:
    row = PushDeviceService(db).acknowledge(
        user_id=context.user.user_id,
        device_id=device_id,
        notification_id=payload.notification_id,
        status=payload.status,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    db.commit()
    return PushDeliveryResponse.model_validate(row)


@app.post("/api/v1/notifications/read", response_model=NotificationSummaryResponse)
def mark_notifications_read(
    payload: NotificationMarkReadRequest,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> NotificationSummaryResponse:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    route = "/api/v1/notifications/read"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id, route, idempotency_key, payload.model_dump(mode="json")
    )
    replay = _find_idempotency(
        db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
    )
    if replay is None:
        NotificationService(db).mark_read(context.user.user_id, payload.notification_ids)
        _remember_idempotency(
            db,
            user_id=context.user.user_id,
            route=route,
            fingerprint=fingerprint,
            resource_type="NOTIFICATION_READ",
            resource_id=context.user.user_id,
        )
        db.commit()
    summary = NotificationService(db).summary(context.user.user_id)
    return NotificationSummaryResponse(
        unread_count=summary["unread_count"],
        high_risk_unread_count=summary["high_risk_unread_count"],
        latest=[NotificationResponse.model_validate(item) for item in summary["latest"]],
    )


@app.post("/api/v1/notifications/read-all", response_model=NotificationSummaryResponse)
def mark_all_notifications_read(
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> NotificationSummaryResponse:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    route = "/api/v1/notifications/read-all"
    fingerprint = _idempotency_fingerprint(context.user.user_id, route, idempotency_key, {})
    replay = _find_idempotency(
        db, user_id=context.user.user_id, route=route, fingerprint=fingerprint
    )
    if replay is None:
        NotificationService(db).mark_all_read(context.user.user_id)
        _remember_idempotency(
            db,
            user_id=context.user.user_id,
            route=route,
            fingerprint=fingerprint,
            resource_type="NOTIFICATION_READ",
            resource_id=context.user.user_id,
        )
        db.commit()
    summary = NotificationService(db).summary(context.user.user_id)
    return NotificationSummaryResponse(
        unread_count=summary["unread_count"],
        high_risk_unread_count=summary["high_risk_unread_count"],
        latest=[NotificationResponse.model_validate(item) for item in summary["latest"]],
    )


@app.get("/api/v1/ai/models", response_model=AIModelOptionsResponse)
def ai_model_options(db: DbSession, _: Current) -> AIModelOptionsResponse:
    runtime = ModelConfigurationService().resolve(db)
    models = (
        []
        if runtime is None
        else list(dict.fromkeys((runtime.search_model, runtime.research_model)))
    )
    return AIModelOptionsResponse(
        models=models,
        reasoning_efforts=["low", "medium", "high", "xhigh"],
        web_search_available=bool(get_effective_settings().searxng_base_url),
    )


@app.get("/api/v1/securities/resolve", response_model=SecurityResolveResponse)
def resolve_security(
    _: Current,
    q: str = Query(min_length=1, max_length=64),
    decision_at: datetime | None = None,
) -> SecurityResolveResponse:
    if decision_at is not None and decision_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="decision_at must include a timezone")
    current = decision_at.astimezone(UTC) if decision_at is not None else datetime.now(UTC)
    if current > datetime.now(UTC) + timedelta(seconds=30):
        raise HTTPException(status_code=422, detail="decision_at must not be in the future")
    result = resolve_security_mentions(f"@{q.strip()}", [], decision_at=current)
    status_item = result.statuses[0] if result.statuses else {}
    status_state = str(status_item.get("state") or "MISSING")
    response_state: Literal["RESOLVED", "UNRESOLVED", "AMBIGUOUS"]
    candidates: list[SecurityResolveCandidate]
    if status_state == "RESOLVED":
        response_state = "RESOLVED"
        candidates = [SecurityResolveCandidate(**item) for item in result.refs]
    elif status_state == "AMBIGUOUS":
        response_state = "AMBIGUOUS"
        candidates = []
    else:
        response_state = "UNRESOLVED"
        candidates = []
    return SecurityResolveResponse(
        query=q.strip(),
        state=response_state,
        candidates=candidates,
        reason_code=str(status_item.get("reason_code") or "SECURITY_MASTER_NOT_FOUND"),
        decision_at=current,
    )


@app.get("/api/v1/ai/chat/metrics", response_model=list[AIChatMetricResponse])
def ai_chat_metrics(db: DbSession, context: Current) -> list[AIChatMetricResponse]:
    return [AIChatMetricResponse(**item) for item in chat_metric_summary(db, context.user.user_id)]


@app.get("/api/v1/ai/costs", response_model=AICostSummaryResponse)
def ai_costs(
    db: DbSession,
    context: Current,
    days: int = Query(default=30, ge=1, le=90),
    limit: int = Query(default=30, ge=1, le=90),
    before: date | None = None,
    thread_id: str | None = None,
) -> AICostSummaryResponse:
    if thread_id is not None:
        owned = db.scalar(
            select(AIChatThread.thread_id).where(
                AIChatThread.thread_id == thread_id,
                AIChatThread.user_id == context.user.user_id,
            )
        )
        if owned is None:
            raise HTTPException(status_code=404, detail="AI chat thread not found")
    return AICostSummaryResponse(
        **chat_cost_summary(
            db,
            context.user.user_id,
            days=days,
            limit=limit,
            before=before,
            thread_id=thread_id,
        )
    )


@app.get("/api/v1/ai/chat/threads", response_model=list[AIChatThreadResponse])
def ai_chat_threads(
    db: DbSession,
    context: Current,
    limit: int = Query(default=30, ge=1, le=100),
    before: datetime | None = None,
) -> list[AIChatThreadResponse]:
    statement = select(AIChatThread).where(
        AIChatThread.user_id == context.user.user_id,
        AIChatThread.archived_at.is_(None),
    )
    if before is not None:
        statement = statement.where(AIChatThread.updated_at < before)
    rows = db.scalars(statement.order_by(AIChatThread.updated_at.desc()).limit(limit)).all()
    return [AIChatThreadResponse.model_validate(row) for row in rows]


@app.get("/api/v1/ai/chat/thread-index", response_model=AIChatThreadIndexResponse)
def ai_chat_thread_index(
    db: DbSession,
    context: Current,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
    archived: bool = False,
    q: str | None = Query(default=None, max_length=128),
) -> AIChatThreadIndexResponse:
    try:
        rows, next_cursor = ChatThreadService(db).list_index(
            user_id=context.user.user_id,
            limit=limit,
            cursor=cursor,
            archived=archived,
            query=q,
        )
    except InvalidThreadCursor as exc:
        raise HTTPException(status_code=422, detail="invalid thread cursor") from exc
    return AIChatThreadIndexResponse(
        items=[AIChatThreadResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@app.post(
    "/api/v1/ai/chat/threads",
    response_model=AIChatThreadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_ai_chat_thread(
    payload: AIChatThreadRequest, db: DbSession, context: Writer
) -> AIChatThreadResponse:
    now = datetime.now(UTC)
    row = AIChatThread(
        user_id=context.user.user_id,
        title=payload.title.strip(),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return AIChatThreadResponse.model_validate(row)


@app.patch("/api/v1/ai/chat/threads/{thread_id}", response_model=AIChatThreadResponse)
def patch_ai_chat_thread(
    thread_id: str,
    payload: AIChatThreadPatchRequest,
    db: DbSession,
    context: Writer,
) -> AIChatThreadResponse:
    try:
        row = ChatThreadService(db).patch(
            context.user.user_id,
            thread_id,
            payload.model_dump(exclude_unset=True),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="AI chat thread not found") from exc
    return AIChatThreadResponse.model_validate(row)


@app.delete("/api/v1/ai/chat/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ai_chat_thread(thread_id: str, db: DbSession, context: Writer) -> Response:
    try:
        ChatThreadService(db).delete(context.user.user_id, thread_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="AI chat thread not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/v1/ai/chat/threads:bulk-delete")
def bulk_delete_ai_chat_threads(
    payload: AIChatBulkDeleteRequest, db: DbSession, context: Writer
) -> dict[str, int]:
    deleted = ChatThreadService(db).bulk_delete(context.user.user_id, payload.thread_ids)
    return {"deleted": deleted}


@app.get(
    "/api/v1/ai/chat/threads/{thread_id}/messages",
    response_model=list[AIChatMessageResponse],
)
def ai_chat_messages(
    thread_id: str,
    db: DbSession,
    context: Current,
    limit: int = Query(default=100, ge=1, le=200),
    before: datetime | None = None,
) -> list[AIChatMessageResponse]:
    owned = db.scalar(
        select(AIChatThread.thread_id).where(
            AIChatThread.thread_id == thread_id,
            AIChatThread.user_id == context.user.user_id,
        )
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="AI chat thread not found")
    statement = select(AIChatMessage).where(AIChatMessage.thread_id == thread_id)
    if before is not None:
        statement = statement.where(AIChatMessage.created_at < before)
    rows = list(db.scalars(statement.order_by(AIChatMessage.created_at.desc()).limit(limit)).all())[
        ::-1
    ]
    return [AIChatMessageResponse.model_validate(row) for row in rows]


@app.post(
    "/api/v1/ai/chat/attachments",
    response_model=list[AIChatAttachmentResponse],
    status_code=status.HTTP_201_CREATED,
)
async def upload_ai_chat_attachments(
    db: DbSession,
    context: Writer,
    files: Annotated[list[UploadFile], File()],
    thread_id: Annotated[str | None, Form()] = None,
) -> list[AIChatAttachmentResponse]:
    if not files or len(files) > MAX_IMAGES_PER_MESSAGE:
        raise HTTPException(status_code=422, detail="each message accepts 1 to 4 images")
    payloads: list[tuple[bytes, str | None]] = []
    total = 0
    try:
        for uploaded in files:
            payload = await uploaded.read(MAX_MESSAGE_IMAGE_BYTES + 1)
            total += len(payload)
            if total > MAX_MESSAGE_IMAGE_BYTES:
                raise AttachmentError(
                    "每条消息的图片合计不能超过 25 MB", code="IMAGE_TOTAL_TOO_LARGE"
                )
            inspect_image(payload, uploaded.content_type)
            payloads.append((payload, uploaded.content_type))
        service = AttachmentService(db)
        rows = [
            service.create(
                user_id=context.user.user_id,
                thread_id=thread_id,
                payload=payload,
                claimed_mime=mime,
            )
            for payload, mime in payloads
        ]
    except AttachmentError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    finally:
        for uploaded in files:
            await uploaded.close()
    return [AIChatAttachmentResponse.model_validate(row) for row in rows]


@app.get("/api/v1/ai/chat/attachments/{attachment_id}/content")
def ai_chat_attachment_content(attachment_id: str, db: DbSession, context: Current) -> Response:
    service = AttachmentService(db)
    row = service.get_owned(context.user.user_id, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="image not found")
    try:
        payload = service.read(context.user.user_id, attachment_id)
    except AttachmentError as exc:
        status_code = 410 if exc.code == "IMAGE_EXPIRED" else 404
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return Response(
        content=payload,
        media_type=row.mime_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _owned_archive_job(db: Session, user_id: str, archive_id: str) -> PersonalArchiveJob:
    row = db.scalar(
        select(PersonalArchiveJob).where(
            PersonalArchiveJob.archive_id == archive_id,
            PersonalArchiveJob.user_id == user_id,
        )
    )
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="personal archive job not found")
    return row


@app.post(
    "/api/v1/me/data-exports",
    response_model=PersonalArchiveJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_personal_data_export(
    payload: PersonalArchiveExportRequest,
    db: DbSession,
    context: Writer,
) -> PersonalArchiveJobResponse:
    now = datetime.now(UTC)
    archive_id = str(uuid4())
    try:
        encrypted_secret = wrap_job_secret(payload.passphrase, archive_id)
    except PersonalArchiveError as exc:
        raise HTTPException(
            status_code=503, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    row = PersonalArchiveJob(
        archive_id=archive_id,
        user_id=context.user.user_id,
        kind="EXPORT",
        status="PENDING",
        phase="QUEUED",
        progress=0,
        encrypted_secret=encrypted_secret,
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    db.add(row)
    db.commit()
    try:
        enqueue_personal_archive(archive_id)
    except Exception as exc:
        row.status = "FAILED"
        row.phase = "FAILED"
        row.error_code = "ARCHIVE_QUEUE_UNAVAILABLE"
        row.encrypted_secret = None
        row.completed_at = datetime.now(UTC)
        db.commit()
        raise HTTPException(status_code=503, detail="personal archive queue unavailable") from exc
    return PersonalArchiveJobResponse.model_validate(row)


@app.get("/api/v1/me/data-exports/{export_id}", response_model=PersonalArchiveJobResponse)
def personal_data_export_status(
    export_id: str, db: DbSession, context: Current
) -> PersonalArchiveJobResponse:
    return PersonalArchiveJobResponse.model_validate(
        _owned_archive_job(db, context.user.user_id, export_id)
    )


@app.get("/api/v1/me/data-exports/{export_id}/download")
def download_personal_data_export(export_id: str, db: DbSession, context: Current) -> Response:
    row = _owned_archive_job(db, context.user.user_id, export_id)
    if row.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=410, detail="personal archive expired")
    if row.status != "SUCCEEDED" or not row.output_object_uri:
        raise HTTPException(status_code=409, detail="personal archive is not ready")
    try:
        payload = read_private_archive(row.output_object_uri)
    except PersonalArchiveError as exc:
        raise HTTPException(status_code=404, detail="personal archive file not found") from exc
    return Response(
        content=payload,
        media_type="application/vnd.ashare.personal-profile",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'attachment; filename="personal-profile.ashare"',
        },
    )


@app.delete("/api/v1/me/data-exports/{export_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_personal_data_export(export_id: str, db: DbSession, context: Writer) -> Response:
    row = db.scalar(
        select(PersonalArchiveJob)
        .where(
            PersonalArchiveJob.archive_id == export_id,
            PersonalArchiveJob.user_id == context.user.user_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="personal archive job not found")
    delete_private_archive(row.source_object_uri)
    delete_private_archive(row.output_object_uri)
    row.deleted_at = datetime.now(UTC)
    row.encrypted_secret = None
    if row.status in {"PENDING", "PROCESSING"}:
        row.status = "CANCELLED"
        row.phase = "DELETED"
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/v1/me/data-imports",
    response_model=PersonalArchiveJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_personal_data_import(
    db: DbSession,
    context: Writer,
    archive: Annotated[UploadFile, File()],
    passphrase: Annotated[str, Form(min_length=8, max_length=128)],
) -> PersonalArchiveJobResponse:
    now = datetime.now(UTC)
    archive_id = str(uuid4())
    try:
        encrypted_secret = wrap_job_secret(passphrase, archive_id)
        path = private_archive_target_path(context.user.user_id, archive_id, "source.ashare")
        total = 0
        with path.open("wb") as handle:
            while chunk := await archive.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise PersonalArchiveError("档案超过大小上限", code="ARCHIVE_TOO_LARGE")
                handle.write(chunk)
        if total == 0:
            raise PersonalArchiveError("档案为空", code="ARCHIVE_FORMAT_INVALID")
    except PersonalArchiveError as exc:
        if "path" in locals() and path.exists():
            path.unlink()
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    finally:
        await archive.close()
    row = PersonalArchiveJob(
        archive_id=archive_id,
        user_id=context.user.user_id,
        kind="IMPORT_PREVIEW",
        status="PENDING",
        phase="QUEUED",
        progress=0,
        encrypted_secret=encrypted_secret,
        source_object_uri=path.as_uri(),
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    db.add(row)
    db.commit()
    try:
        enqueue_personal_archive(archive_id)
    except Exception as exc:
        row.status = "FAILED"
        row.phase = "FAILED"
        row.error_code = "ARCHIVE_QUEUE_UNAVAILABLE"
        row.encrypted_secret = None
        row.completed_at = datetime.now(UTC)
        db.commit()
        raise HTTPException(status_code=503, detail="personal archive queue unavailable") from exc
    return PersonalArchiveJobResponse.model_validate(row)


@app.get("/api/v1/me/data-imports/{import_id}", response_model=PersonalArchiveJobResponse)
def personal_data_import_status(
    import_id: str, db: DbSession, context: Current
) -> PersonalArchiveJobResponse:
    row = _owned_archive_job(db, context.user.user_id, import_id)
    if row.kind not in {"IMPORT_PREVIEW", "IMPORT_APPLY"}:
        raise HTTPException(status_code=404, detail="personal import job not found")
    return PersonalArchiveJobResponse.model_validate(row)


@app.post(
    "/api/v1/me/data-imports/{import_id}/apply",
    response_model=PersonalArchiveJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def apply_personal_data_import(
    import_id: str,
    payload: PersonalArchiveApplyRequest,
    response: Response,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> PersonalArchiveJobResponse:
    preview = _owned_archive_job(db, context.user.user_id, import_id)
    if preview.kind != "IMPORT_PREVIEW" or preview.status != "SUCCEEDED":
        raise HTTPException(status_code=409, detail="import preview is not ready")
    if preview.expires_at <= datetime.now(UTC) or not preview.source_object_uri:
        raise HTTPException(status_code=410, detail="import preview expired")
    route = f"/api/v1/me/data-imports/{import_id}/apply"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id, route, idempotency_key, payload.model_dump(mode="json")
    )
    replay = _find_idempotency(
        db,
        user_id=context.user.user_id,
        route=route,
        fingerprint=fingerprint,
    )
    if replay is not None:
        row = _owned_archive_job(db, context.user.user_id, replay.resource_id)
        response.status_code = status.HTTP_200_OK
        return PersonalArchiveJobResponse.model_validate(row)
    now = datetime.now(UTC)
    job_id = str(uuid4())
    row = PersonalArchiveJob(
        archive_id=job_id,
        user_id=context.user.user_id,
        kind="IMPORT_APPLY",
        status="PENDING",
        phase="QUEUED",
        progress=0,
        encrypted_secret=preview.encrypted_secret,
        source_object_uri=preview.source_object_uri,
        source_archive_id=preview.archive_id,
        merge_options=payload.merge_options,
        created_at=now,
        expires_at=preview.expires_at,
    )
    db.add(row)
    _remember_idempotency(
        db,
        user_id=context.user.user_id,
        route=route,
        fingerprint=fingerprint,
        resource_type="PERSONAL_ARCHIVE",
        resource_id=job_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        replay = _find_idempotency(
            db,
            user_id=context.user.user_id,
            route=route,
            fingerprint=fingerprint,
        )
        if replay is not None:
            response.status_code = status.HTTP_200_OK
            return PersonalArchiveJobResponse.model_validate(
                _owned_archive_job(db, context.user.user_id, replay.resource_id)
            )
        raise HTTPException(status_code=409, detail="import apply conflicted") from exc
    try:
        enqueue_personal_archive(job_id)
    except Exception as exc:
        row.status = "FAILED"
        row.phase = "FAILED"
        row.error_code = "ARCHIVE_QUEUE_UNAVAILABLE"
        row.completed_at = datetime.now(UTC)
        db.commit()
        raise HTTPException(status_code=503, detail="personal archive queue unavailable") from exc
    return PersonalArchiveJobResponse.model_validate(row)


@app.post("/api/v1/ai/chat/threads/{thread_id}/messages:stream")
def stream_ai_chat_message(
    thread_id: str,
    payload: AIChatSendRequest,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> StreamingResponse:
    if payload.decision_at is not None:
        if payload.decision_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="decision_at must include a timezone")
        if payload.decision_at.astimezone(UTC) > datetime.now(UTC) + timedelta(seconds=30):
            raise HTTPException(status_code=422, detail="decision_at must not be in the future")
    owned = db.scalar(
        select(AIChatThread.thread_id).where(
            AIChatThread.thread_id == thread_id,
            AIChatThread.user_id == context.user.user_id,
        )
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="AI chat thread not found")
    effective_key = idempotency_key or str(uuid4())
    existing = db.scalar(
        select(AIChatMessage.message_id).where(
            AIChatMessage.thread_id == thread_id,
            AIChatMessage.idempotency_key_sha256 == sha256_bytes(effective_key.encode()),
        )
    )
    if existing is None and not allow_chat_request(context.user.user_id):
        raise HTTPException(
            status_code=429,
            detail="AI chat rate limit exceeded",
            headers={"Retry-After": "60"},
        )

    async def events() -> AsyncIterator[str]:
        request_id = str(uuid4())
        try:
            async for event in stream_chat_response(
                user_id=context.user.user_id,
                thread_id=thread_id,
                content=payload.content,
                model=payload.model,
                reasoning_effort=payload.reasoning_effort,
                web_search=payload.web_search,
                attachment_ids=payload.attachment_ids,
                mention_refs=[item.model_dump() for item in payload.mention_refs],
                decision_at=payload.decision_at,
                idempotency_key=effective_key,
                request_id=request_id,
            ):
                yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ChatStreamError as exc:
            logger.warning(
                "AI chat stream failed type=%s code=%s upstream_status=%s",
                type(exc).__name__,
                exc.code,
                exc.status_code,
            )
            event = {
                "type": "error",
                "code": exc.code,
                "message": str(exc),
                "request_id": exc.request_id,
                "retryable": exc.retryable,
            }
            yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ValueError as exc:
            event = {
                "type": "error",
                "code": "CHAT_REQUEST_INVALID",
                "message": str(exc),
                "request_id": request_id,
                "retryable": False,
            }
            yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("AI chat stream failed type=%s", type(exc).__name__)
            event = {
                "type": "error",
                "code": "CHAT_INTERNAL_ERROR",
                "message": "AI 对话生成失败",
                "request_id": request_id,
                "retryable": False,
            }
            yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _list_users(db: Session, context: AuthContext) -> list[UserResponse]:
    _admin(context)
    rows = db.scalars(select(UserAccount).order_by(UserAccount.username)).all()
    return [UserResponse.model_validate(row) for row in rows]


@app.get("/api/v1/admin/users", response_model=list[UserResponse])
@app.get("/api/v1/users", response_model=list[UserResponse], include_in_schema=False)
def users(db: DbSession, context: Current) -> list[UserResponse]:
    return _list_users(db, context)


@app.post("/api/v1/admin/users", response_model=UserResponse, status_code=201)
@app.post("/api/v1/users", response_model=UserResponse, status_code=201, include_in_schema=False)
def create_user(payload: UserCreateRequest, db: DbSession, context: Writer) -> UserResponse:
    _admin(context)
    now = datetime.now(UTC)
    row = UserAccount(
        username=payload.username.strip().casefold(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="username already exists") from exc
    db.refresh(row)
    return UserResponse.model_validate(row)


def _change_user(
    user_id: str, payload: UserUpdateRequest, db: Session, context: AuthContext
) -> UserResponse:
    _admin(context)
    row = db.get(UserAccount, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    invalidates = False
    if payload.enabled is not None and payload.enabled != row.enabled:
        if row.user_id == context.user.user_id and not payload.enabled:
            raise HTTPException(status_code=409, detail="cannot disable current administrator")
        row.enabled = payload.enabled
        invalidates = True
    if payload.role is not None and payload.role != row.role:
        if row.user_id == context.user.user_id and payload.role != "ADMIN":
            raise HTTPException(status_code=409, detail="cannot demote current administrator")
        row.role = payload.role
        invalidates = True
    if payload.password is not None:
        row.password_hash = hash_password(payload.password)
        invalidates = True
    if invalidates:
        invalidate_user_sessions(db, row)
    row.updated_at = datetime.now(UTC)
    db.commit()
    return UserResponse.model_validate(row)


@app.patch("/api/v1/admin/users/{user_id}", response_model=UserResponse)
@app.patch("/api/v1/users/{user_id}", response_model=UserResponse, include_in_schema=False)
def update_user(
    user_id: str, payload: UserUpdateRequest, db: DbSession, context: Writer
) -> UserResponse:
    return _change_user(user_id, payload, db, context)


@app.post("/api/v1/admin/users/{user_id}/password", response_model=UserResponse)
@app.post("/api/v1/users/{user_id}/password", response_model=UserResponse, include_in_schema=False)
def reset_password(
    user_id: str, payload: PasswordResetRequest, db: DbSession, context: Writer
) -> UserResponse:
    return _change_user(user_id, UserUpdateRequest(password=payload.password), db, context)


def _model_draft(payload: ModelSettingsRequest) -> ModelSettingsDraft:
    values = payload.model_dump(exclude={"model_profiles"})
    values["model_profiles"] = tuple(
        ModelProfileDraft(**item.model_dump()) for item in payload.model_profiles
    )
    return ModelSettingsDraft(**values)


def _model_settings_response(db: Session) -> ModelSettingsResponse:
    service = ModelConfigurationService()
    runtime = service.resolve(db, require_enabled=False)
    if runtime is None:
        return ModelSettingsResponse(
            configuration_id=None,
            version=0,
            config_sha256="",
            source="none",
            provider="openai-compatible",
            base_url="",
            api_key_configured=False,
            search_model="gpt-5.6-luna",
            search_reasoning_effort="low",
            research_model="gpt-5.6-sol",
            research_reasoning_effort="high",
            model_profiles=[],
            timeout_seconds=90,
            enabled=False,
            configured=False,
            reachable=False,
            degraded=False,
            status_message="尚未配置模型 API",
            structured_output_supported=False,
            streaming_supported=False,
        )
    health = service.status(db)
    return ModelSettingsResponse(
        configuration_id=runtime.configuration_id,
        version=runtime.version,
        config_sha256=runtime.config_sha256,
        source=runtime.source,
        provider=runtime.provider,
        base_url=runtime.base_url,
        api_key_configured=True,
        search_model=runtime.search_model,
        search_reasoning_effort=runtime.search_reasoning_effort,
        research_model=runtime.research_model,
        research_reasoning_effort=runtime.research_reasoning_effort,
        model_profiles=[
            ModelProfileSettings.model_validate(profile.public_dict())
            for profile in runtime.model_profiles
        ],
        timeout_seconds=runtime.timeout_seconds,
        enabled=runtime.enabled,
        configured=bool(health["configured"]),
        reachable=bool(health["reachable"]),
        degraded=bool(health["degraded"]),
        status_message=str(health["message"]),
        checked_at=(health["checked_at"] if isinstance(health["checked_at"], datetime) else None),
        structured_output_supported=bool(health.get("structured_output_supported")),
        streaming_supported=bool(health.get("streaming_supported")),
    )


_SYSTEM_QUEUE_KEYS = {
    "personal_archive": ("ashare:personal-archive:pending", "ashare:personal-archive:processing"),
    "research": ("ashare:research:pending", "ashare:research:processing"),
    "trade_plan": ("ashare:trade-plan:pending", "ashare:trade-plan:processing"),
    "backtest": ("ashare:backtest:pending", "ashare:backtest:processing"),
}


def _system_worker_snapshot(
    topology_sha256: str,
) -> tuple[str, bool, list[dict[str, object]], dict[str, dict[str, int]]]:
    """Read best-effort operator data without making settings availability depend on Redis."""
    try:
        import redis

        client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
        heartbeats = read_heartbeats(client)
        queues = {
            name: {
                "pending": int(client.llen(pending)),
                "processing": int(client.llen(processing)),
            }
            for name, (pending, processing) in _SYSTEM_QUEUE_KEYS.items()
        }
    except Exception:
        heartbeats, queues = (
            [],
            {name: {"pending": 0, "processing": 0} for name in _SYSTEM_QUEUE_KEYS},
        )
    job_workers = [item for item in heartbeats if item.get("role") == "job-worker"]
    modes = {str(item.get("loaded_mode")) for item in job_workers}
    actual_mode = (
        next(iter(modes)) if len(modes) == 1 and modes <= {"SERIAL", "DUAL"} else "UNKNOWN"
    )
    matching_job = any(item.get("topology_sha256") == topology_sha256 for item in job_workers)
    return actual_mode, not matching_job, heartbeats, queues


def _system_settings_response(db: Session) -> SystemSettingsResponse:
    view = SystemConfigurationService().public_view(db)
    saved_values = cast(dict[str, Any], view["values"])
    actual_mode, restart_required, workers, queues = _system_worker_snapshot(
        str(view["topology_sha256"])
    )
    return SystemSettingsResponse.model_validate(
        {
            **view,
            "actual_loaded_mode": actual_mode,
            "restart_required": restart_required,
            "workers": workers,
            "queues": queues,
            "compose_restart_command": _system_settings_restart_command(
                str(saved_values["research_execution_mode"])
            ),
        }
    )


def _system_settings_restart_command(execution_mode: str) -> str:
    """Return the operator command required to apply a persisted topology.

    Compose profiles are evaluated before a container runs, whereas the
    execution mode is stored in PostgreSQL.  The API deliberately has no
    Docker socket, so it gives the operator the exact command instead.
    """

    prefix = "docker compose -p ashare-ai-src -f compose.yaml"
    if execution_mode == "DUAL":
        return f"{prefix} --profile dual-research up -d --force-recreate job-worker research-worker"
    return (
        f"{prefix} --profile dual-research stop research-worker; "
        f"{prefix} up -d --force-recreate job-worker"
    )


def _system_settings_payload(
    payload: SystemSettingsRequest,
) -> tuple[dict[str, Any], dict[str, str]]:
    values = payload.model_dump(exclude_unset=True)
    secrets = {
        field: value
        for field, value in values.items()
        if field in SECRET_SETTING_FIELDS and isinstance(value, str)
    }
    public = {field: value for field, value in values.items() if field not in SECRET_SETTING_FIELDS}
    return public, secrets


@app.get("/api/v1/admin/model-settings", response_model=ModelSettingsResponse)
def get_model_settings(db: DbSession, context: Current) -> ModelSettingsResponse:
    _admin(context)
    try:
        return _model_settings_response(db)
    except ModelSettingsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.put("/api/v1/admin/model-settings", response_model=ModelSettingsResponse)
async def put_model_settings(
    payload: ModelSettingsRequest, db: DbSession, context: Writer
) -> ModelSettingsResponse:
    _admin(context)
    service = ModelConfigurationService()
    draft = _model_draft(payload)
    try:
        probe = await service.probe(draft, db) if draft.enabled else None
        service.save_and_activate(db, draft, user_id=context.user.user_id, probe=probe)
        db.commit()
        return _model_settings_response(db)
    except ModelSettingsError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/admin/model-settings/test", response_model=ModelProbeResponse)
async def test_model_settings(
    payload: ModelSettingsRequest, db: DbSession, context: Writer
) -> ModelProbeResponse:
    _admin(context)
    try:
        result = await ModelConfigurationService().probe(_model_draft(payload), db)
        return ModelProbeResponse(**result.__dict__)
    except ModelSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/admin/model-settings/models", response_model=ModelListResponse)
async def list_model_settings_models(
    payload: ModelSettingsRequest, db: DbSession, context: Writer
) -> ModelListResponse:
    _admin(context)
    try:
        models = await ModelConfigurationService().list_models(_model_draft(payload), db)
        return ModelListResponse(models=models)
    except ModelSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/admin/system-settings", response_model=SystemSettingsResponse)
def get_system_settings(db: DbSession, context: Current) -> SystemSettingsResponse:
    _admin(context)
    try:
        return _system_settings_response(db)
    except SystemSettingsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/admin/system-resources", response_model=SystemResourcesResponse)
def get_system_resources(context: Current) -> SystemResourcesResponse:
    _admin(context)
    try:
        _, _, workers, _ = _system_worker_snapshot("")
        return SystemResourcesResponse.model_validate(sample_runtime_resources(workers))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="runtime resources unavailable") from exc


@app.post(
    "/api/v1/admin/system-settings/unlock",
    response_model=SystemSettingsUnlockResponse,
)
def unlock_system_settings(
    payload: SystemSettingsUnlockRequest,
    request: Request,
    context: Writer,
) -> SystemSettingsUnlockResponse:
    _admin(context)
    token, expires_at = issue_unlock(request, context, payload.password)
    return SystemSettingsUnlockResponse(unlock_token=token, expires_at=expires_at)


@app.put("/api/v1/admin/system-settings", response_model=SystemSettingsResponse)
def put_system_settings(
    payload: SystemSettingsRequest,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
    unlock_token: SystemSettingsUnlockToken = None,
) -> SystemSettingsResponse:
    _admin(context)
    require_settings_unlock(context, unlock_token)
    public, secrets = _system_settings_payload(payload)
    if not public and not secrets:
        raise HTTPException(status_code=422, detail="submit at least one system setting")
    body = payload.model_dump(mode="json", exclude_unset=True)
    fingerprint = _idempotency_fingerprint(
        context.user.user_id, "/api/v1/admin/system-settings", idempotency_key, body
    )
    replay = _find_idempotency(
        db,
        user_id=context.user.user_id,
        route="/api/v1/admin/system-settings",
        fingerprint=fingerprint,
    )
    if replay is not None:
        return _system_settings_response(db)
    try:
        runtime = SystemConfigurationService().save(
            db,
            public_updates=public,
            secret_updates=secrets,
            user_id=context.user.user_id,
        )
        _remember_idempotency(
            db,
            user_id=context.user.user_id,
            route="/api/v1/admin/system-settings",
            fingerprint=fingerprint,
            resource_type="SYSTEM_CONFIGURATION",
            resource_id=runtime.configuration_id or "environment",
        )
        db.commit()
        # Cached adapters are recreated on the next request so non-topology
        # values (search, market and storage tuning) hot-load without a Docker
        # restart.  Worker topology itself is intentionally boot-time only.
        reset_market_data_service()
        get_market_data_service().start()
        get_financial_search_service.cache_clear()
        logger.info(
            "administrator saved system configuration version=%s hash=%s",
            runtime.version,
            runtime.config_sha256,
        )
        return _system_settings_response(db)
    except SystemSettingsError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/v1/admin/system-settings/{field}", response_model=SystemSettingsResponse)
def restore_system_setting(
    field: str,
    db: DbSession,
    context: Writer,
    unlock_token: SystemSettingsUnlockToken = None,
) -> SystemSettingsResponse:
    _admin(context)
    require_settings_unlock(context, unlock_token)
    try:
        SystemConfigurationService().restore_field(db, field=field, user_id=context.user.user_id)
        db.commit()
        reset_market_data_service()
        get_market_data_service().start()
        get_financial_search_service.cache_clear()
        return _system_settings_response(db)
    except SystemSettingsError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/v1/admin/system-settings", response_model=SystemSettingsResponse)
def restore_all_system_settings(
    db: DbSession,
    context: Writer,
    unlock_token: SystemSettingsUnlockToken = None,
) -> SystemSettingsResponse:
    _admin(context)
    require_settings_unlock(context, unlock_token)
    try:
        SystemConfigurationService().restore_all(db, user_id=context.user.user_id)
        db.commit()
        reset_market_data_service()
        get_market_data_service().start()
        get_financial_search_service.cache_clear()
        return _system_settings_response(db)
    except SystemSettingsError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/scores/{trading_date}", response_model=list[ScoreResponse])
def scores(
    trading_date: date, db: DbSession, context: Current, run_id: str | None = None
) -> list[ScoreResponse]:
    return [
        ScoreResponse.model_validate(row)
        for row in QueryRepository(db).scores(
            trading_date, run_id=run_id, **_result_access(context)
        )
    ]


@app.get("/api/v1/scores/{trading_date}/{symbol}", response_model=ScoreResponse)
def score(
    trading_date: date, symbol: str, db: DbSession, context: Current, run_id: str | None = None
) -> ScoreResponse:
    row = QueryRepository(db).score(
        trading_date, symbol.upper(), run_id=run_id, **_result_access(context)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="score not found")
    return ScoreResponse.model_validate(row)


@app.get("/api/v1/scores/{trading_date}/{symbol}/lineage")
def score_lineage(
    trading_date: date, symbol: str, db: DbSession, context: Current, run_id: str | None = None
) -> dict[str, Any]:
    repository = QueryRepository(db)
    row = repository.score(trading_date, symbol.upper(), run_id=run_id, **_result_access(context))
    if row is None:
        raise HTTPException(status_code=404, detail="score not found")
    return {
        "symbol": row.symbol,
        "trading_date": row.trading_date,
        "run_id": row.run_id,
        "feature_snapshot_id": row.feature_snapshot_id,
        "agent_bundle_sha256": row.agent_bundle_sha256,
        "evidence_bundle_sha256": row.evidence_bundle_sha256,
        "formula_version": row.formula_version,
        "base_total_score": row.base_total_score,
        "dividend_bonus": row.dividend_bonus,
        "event_risk_multiplier": row.event_risk_multiplier,
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "component": item.component,
                "evidence_type": item.evidence_type,
                "source": item.source,
                "source_record_id": item.source_record_id,
                "available_at": item.available_at,
                "payload_sha256": item.payload_sha256,
                "excerpt": item.excerpt,
                "object_uri": item.object_uri,
            }
            for item in repository.evidence(row.run_id, row.symbol)
        ],
    }


@app.get("/api/v1/candidates/{trading_date}", response_model=list[CandidateResponse])
def candidates(
    trading_date: date, db: DbSession, context: Current, run_id: str | None = None
) -> list[CandidateResponse]:
    rows = QueryRepository(db).candidates(trading_date, run_id=run_id, **_result_access(context))
    run = db.get(JobRun, rows[0].run_id) if rows else None
    names = (
        _security_names_at(db, {row.symbol for row in rows}, run.decision_at)
        if run is not None
        else {}
    )
    scores = (
        {
            (item.run_id, item.symbol): item
            for item in db.scalars(
                select(ScoreRow).where(
                    ScoreRow.run_id.in_({row.run_id for row in rows}),
                    ScoreRow.symbol.in_({row.symbol for row in rows}),
                )
            ).all()
        }
        if rows
        else {}
    )
    return [
        CandidateResponse.model_validate(
            {
                **{column.name: getattr(row, column.name) for column in row.__table__.columns},
                "name": names.get(row.symbol),
                "base_total_score": (
                    scores[(row.run_id, row.symbol)].base_total_score
                    if (row.run_id, row.symbol) in scores
                    else row.total_score
                ),
                "dividend_bonus": (
                    scores[(row.run_id, row.symbol)].dividend_bonus
                    if (row.run_id, row.symbol) in scores
                    else 0
                ),
            }
        )
        for row in rows
    ]


@app.get("/api/v1/portfolios/{trading_date}", response_model=PortfolioResponse)
def portfolio(
    trading_date: date, db: DbSession, context: Current, run_id: str | None = None
) -> PortfolioResponse:
    repository = QueryRepository(db)
    selected_run_id = repository.result_run_id(trading_date, run_id, **_result_access(context))
    selected_run = db.get(JobRun, selected_run_id) if selected_run_id else None
    if selected_run is not None and selected_run.status == "FUSED":
        manifest = dict(selected_run.manifest or {})
        gate = manifest.get("data_quality_gate")
        gate = gate if isinstance(gate, dict) else {}
        risk_outcome = manifest.get("risk_outcome")
        risk_outcome = risk_outcome if isinstance(risk_outcome, dict) else {}
        return PortfolioResponse(
            run_id=selected_run.run_id,
            trading_date=trading_date,
            status="FUSED",
            observation_only=True,
            message=str(
                risk_outcome.get("reason_message") or "正式组合条件未满足，当前仅发布观察报告"
            ),
            reason_code=(
                str(risk_outcome["reason_code"]) if risk_outcome.get("reason_code") else None
            ),
            formal_eligible_symbols=list(gate.get("formal_eligible_symbols", [])),
            excluded_symbols=dict(gate.get("excluded_symbols", {})),
        )
    row = repository.portfolio(trading_date, run_id=run_id, **_result_access(context))
    if row is None:
        manifest = dict(selected_run.manifest or {}) if selected_run is not None else {}
        outcome = manifest.get("portfolio_outcome")
        outcome = outcome if isinstance(outcome, dict) else {}
        gate = manifest.get("data_quality_gate")
        gate = gate if isinstance(gate, dict) else {}
        if (
            selected_run is not None
            and selected_run.status == "SUCCEEDED"
            and (
                not bool(manifest.get("portfolio_requested", True))
                or outcome.get("generated") is False
            )
        ):
            reason = manifest.get("research_only_reason")
            failure = outcome.get("reason")
            if not reason and isinstance(failure, dict):
                reason = failure.get("message")
            return PortfolioResponse(
                run_id=selected_run.run_id,
                trading_date=trading_date,
                status="SUCCEEDED",
                research_only=True,
                message=str(
                    outcome.get("reason_message") or reason or "本次研究未请求生成模拟组合"
                ),
                reason_code=(str(outcome["reason_code"]) if outcome.get("reason_code") else None),
                formal_eligible_symbols=list(gate.get("formal_eligible_symbols", [])),
                excluded_symbols=dict(gate.get("excluded_symbols", {})),
            )
        raise HTTPException(status_code=404, detail="portfolio not found")
    return PortfolioResponse.model_validate(row)


@app.get("/api/v1/reports/{trading_date}", response_model=ReportResponse)
def report(
    trading_date: date, db: DbSession, context: Current, run_id: str | None = None
) -> ReportResponse:
    row = QueryRepository(db).report(trading_date, run_id=run_id, **_result_access(context))
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return ReportResponse.model_validate(row)


@app.get("/api/v1/reports/{report_id}/content", response_model=ReportBodyResponse)
def report_content(report_id: str, db: DbSession, context: Current) -> ReportBodyResponse:
    row = db.get(ReportRow, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    run = db.get(JobRun, row.run_id)
    if run is None or not _owns(run, context):
        raise HTTPException(status_code=404, detail="report not found")
    settings = get_effective_settings()
    if row.object_uri.startswith("s3://"):
        store: ObjectStore = S3ObjectStore(
            bucket=settings.object_store_bucket,
            endpoint_url=settings.object_store_endpoint,
            access_key=settings.object_store_access_key,
            secret_key=settings.object_store_secret_key,
            secure=settings.object_store_secure,
        )
    else:
        store = LocalObjectStore(settings.lake_root.parent / "objects")
    try:
        content = store.get(row.object_uri).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="report content unavailable") from exc
    return ReportBodyResponse(report_id=row.report_id, content_type="text/html", content=content)


@app.get("/api/v1/reports/{report_id}/symbols", response_model=list[ReportSymbolResponse])
def report_symbols(report_id: str, db: DbSession, context: Current) -> list[ReportSymbolResponse]:
    report_row = db.get(ReportRow, report_id)
    if report_row is None:
        raise HTTPException(status_code=404, detail="report not found")
    run = db.get(JobRun, report_row.run_id)
    if run is None or not _owns(run, context):
        raise HTTPException(status_code=404, detail="report not found")
    manifest = dict(run.manifest or {})
    gate = manifest.get("data_quality_gate")
    gate = gate if isinstance(gate, dict) else {}
    gate_declared = "formal_eligible_symbols" in gate
    eligible = set(str(item) for item in gate.get("formal_eligible_symbols", []))
    raw_excluded = gate.get("excluded_symbols")
    raw_excluded = raw_excluded if isinstance(raw_excluded, dict) else {}
    candidates = {
        item.symbol: item
        for item in db.scalars(select(CandidateRow).where(CandidateRow.run_id == run.run_id)).all()
    }
    scores = list(
        db.scalars(
            select(ScoreRow).where(ScoreRow.run_id == run.run_id).order_by(ScoreRow.symbol)
        ).all()
    )
    quality = manifest.get("symbol_data_quality")
    quality = quality if isinstance(quality, dict) else {}
    names = _security_names_at(db, {score.symbol for score in scores}, run.decision_at)
    global_fused = run.status == "FUSED"
    result: list[ReportSymbolResponse] = []
    for score in scores:
        candidate = candidates.get(score.symbol)
        reasons = [str(item) for item in raw_excluded.get(score.symbol, [])]
        if global_fused:
            risk = manifest.get("risk_outcome")
            risk = risk if isinstance(risk, dict) else {}
            reasons = list(
                dict.fromkeys([*reasons, str(risk.get("reason_code") or "GLOBAL_RISK_FUSE_ACTIVE")])
            )
        elif score.event_risk_multiplier <= 0:
            reasons = list(dict.fromkeys([*reasons, "CRITICAL_EVENT_RISK"]))
        advice_eligible = (
            not global_fused
            and (score.symbol in eligible if gate_declared else candidate is not None)
            and score.event_risk_multiplier > 0
            and candidate is not None
        )
        status_value: Literal["FORMAL", "FORMAL_WITH_LIMITATIONS", "RISK_BLOCKED"] = (
            "FORMAL"
            if advice_eligible
            else "RISK_BLOCKED"
            if global_fused or score.event_risk_multiplier <= 0
            else "FORMAL_WITH_LIMITATIONS"
        )
        result.append(
            ReportSymbolResponse(
                symbol=score.symbol,
                name=names.get(score.symbol),
                research_status=status_value,
                advice_eligible=advice_eligible,
                recommendation=None if advice_eligible else "NO_BUY",
                exclusion_reasons=reasons,
                data_quality=(
                    quality.get(score.symbol, {})
                    if isinstance(quality.get(score.symbol), dict)
                    else {}
                ),
                score=ScoreResponse.model_validate(score),
                rank=candidate.rank if candidate else None,
                prediction_percentile=candidate.prediction_percentile if candidate else None,
                industry_code=candidate.industry_code if candidate else None,
                industry_name=candidate.industry_name if candidate else None,
                plain_language_summary=symbol_summary(
                    total_score=score.total_score,
                    fundamental_score=score.fundamental_score,
                    technical_score=score.technical_score,
                    sentiment_score=score.sentiment_score,
                    advice_eligible=advice_eligible,
                    reasons=reasons,
                ),
                component_summaries={
                    "fundamental": component_summary("fundamental", score.fundamental_score),
                    "technical": component_summary("technical", score.technical_score),
                    "sentiment": component_summary("sentiment", score.sentiment_score),
                },
            )
        )
    return result


@app.get(
    "/api/v1/reports/{report_id}/execution-status",
    response_model=ReportExecutionStatusResponse,
)
def report_execution_status(
    report_id: str, db: DbSession, context: Current
) -> ReportExecutionStatusResponse:
    report_row = db.get(ReportRow, report_id)
    if report_row is None:
        raise HTTPException(status_code=404, detail="report not found")
    run = db.get(JobRun, report_row.run_id)
    if run is None or not _owns(run, context):
        raise HTTPException(status_code=404, detail="report not found")
    assets = db.get(UserAssetState, context.user.user_id)
    positions = {
        str(item.get("symbol", "")).upper(): dict(item)
        for item in (assets.positions if assets is not None else [])
        if item.get("symbol")
    }
    symbols = list(
        db.scalars(
            select(ScoreRow.symbol).where(ScoreRow.run_id == run.run_id).order_by(ScoreRow.symbol)
        )
    )
    as_of = datetime.now(UTC)
    items = []
    for symbol in symbols:
        position = positions.get(symbol)
        if position is None:
            continue
        eligibility = position_sellability(position, trading_date=as_of.astimezone(SHANGHAI).date())
        items.append(
            ReportExecutionSymbolStatus(
                symbol=symbol,
                held_quantity=eligibility.held_quantity,
                acquired_on=eligibility.acquired_on,
                sellable_quantity=eligibility.sellable_quantity,
                t1_restricted=eligibility.t1_restricted,
                blockers=list(eligibility.blockers),
            )
        )
    return ReportExecutionStatusResponse(report_id=report_id, as_of=as_of, items=items)


@app.post(
    "/api/v1/reports/{report_id}/trade-plans",
    response_model=TradePlanResponse,
    status_code=202,
)
def submit_trade_plan(
    report_id: str,
    payload: TradePlanRequest,
    response: Response,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> TradePlanResponse:
    idempotency_route = f"/api/v1/reports/{report_id}/trade-plans"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id,
        idempotency_route,
        idempotency_key,
        payload.model_dump(mode="json"),
    )
    replay = _find_idempotency(
        db,
        user_id=context.user.user_id,
        route=idempotency_route,
        fingerprint=fingerprint,
    )
    if replay is not None:
        existing_plan = db.get(TradePlanRow, replay.resource_id)
        if (
            replay.resource_type != "TRADE_PLAN"
            or existing_plan is None
            or existing_plan.user_id != context.user.user_id
        ):
            raise HTTPException(status_code=409, detail="idempotent resource is unavailable")
        response.status_code = status.HTTP_200_OK
        return TradePlanResponse.model_validate(existing_plan)
    report_row = db.get(ReportRow, report_id)
    if report_row is None:
        raise HTTPException(status_code=404, detail="report not found")
    run = db.get(JobRun, report_row.run_id)
    if run is None or not _owns(run, context):
        raise HTTPException(status_code=404, detail="report not found")
    if run.status != "SUCCEEDED":
        _trade_plan_error(
            "GLOBAL_RISK_FUSE_ACTIVE",
            "全局风控熔断或研究尚未成功，不能生成购买建议",
        )
    manifest = dict(run.manifest or {})
    target_symbols = set(str(item) for item in manifest.get("target_symbols", []))
    if target_symbols:
        outside = sorted(set(payload.symbols) - target_symbols)
        if outside:
            _trade_plan_error("SYMBOL_NOT_IN_REPORT", "股票不属于本次研究范围", symbols=outside)
    candidates = list(
        db.scalars(
            select(CandidateRow).where(
                CandidateRow.run_id == run.run_id,
                CandidateRow.symbol.in_(payload.symbols),
            )
        ).all()
    )
    by_symbol = {item.symbol: item for item in candidates}
    missing = sorted(set(payload.symbols) - set(by_symbol))
    if missing:
        gate = manifest.get("data_quality_gate")
        gate = gate if isinstance(gate, dict) else {}
        excluded = gate.get("excluded_symbols")
        excluded = excluded if isinstance(excluded, dict) else {}
        incomplete = [symbol for symbol in missing if symbol in excluded]
        if incomplete:
            _trade_plan_error(
                "SYMBOL_DATA_INCOMPLETE", "股票未通过个股数据完整性门禁", symbols=incomplete
            )
        _trade_plan_error("SYMBOL_NOT_IN_REPORT", "股票不是本次报告的正式研究对象", symbols=missing)
    blocked = sorted(
        symbol for symbol, item in by_symbol.items() if item.event_risk_multiplier <= 0
    )
    if blocked:
        _trade_plan_error(
            "CRITICAL_EVENT_RISK", "股票存在重大事件风险，固定为 NO_BUY", symbols=blocked
        )
    gate = manifest.get("data_quality_gate", {})
    formal = set(gate.get("formal_eligible_symbols", [])) if isinstance(gate, dict) else set()
    incomplete = sorted(set(payload.symbols) - formal) if formal else []
    if incomplete:
        _trade_plan_error(
            "SYMBOL_DATA_INCOMPLETE", "股票未通过个股数据完整性门禁", symbols=incomplete
        )
    snapshot = db.scalar(
        select(SnapshotManifestRow)
        .where(
            SnapshotManifestRow.run_id == run.run_id,
            SnapshotManifestRow.dataset == "backtest_bundle",
            SnapshotManifestRow.status == "COMMITTED",
        )
        .order_by(SnapshotManifestRow.committed_at.desc())
        .limit(1)
    )
    if snapshot is None:
        _trade_plan_error("INSUFFICIENT_VALIDATION_HISTORY", "报告没有已提交的个股验证快照")
    if len(snapshot.details.get("future_trading_dates", [])) < 3:
        _trade_plan_error("INSUFFICIENT_VALIDATION_HISTORY", "验证快照缺少三个已冻结的未来交易日")
    request_payload = {
        **payload.model_dump(mode="json"),
        "optimizer_policy": _trade_plan_policy_payload(run),
    }
    active_key = stable_hash(
        {
            "kind": "ACTIVE_TRADE_PLAN",
            "user_id": context.user.user_id,
            "report_id": report_id,
            "symbols": payload.symbols,
            "budget_override": payload.budget_override,
            "objective": payload.objective,
        }
    )
    existing = db.scalar(
        select(TradePlanRow).where(TradePlanRow.active_trade_plan_key == active_key)
    )
    if existing is not None and existing.status in {"PENDING", "RUNNING"}:
        response.status_code = 200
        return TradePlanResponse.model_validate(existing)
    now = datetime.now(UTC)
    model_reference = (
        run.manifest.get("model_configuration") if isinstance(run.manifest, dict) else None
    )
    row = TradePlanRow(
        user_id=context.user.user_id,
        report_id=report_id,
        run_id=run.run_id,
        trading_date=run.trading_date,
        decision_at=run.decision_at,
        available_at=now,
        status="PENDING",
        objective=payload.objective,
        symbols=payload.symbols,
        budget_override=payload.budget_override,
        request_payload=request_payload,
        snapshot_ids=[snapshot.snapshot_id],
        optimizer_version="trade-plan-grid-oos-v1",
        config_version=str(run.manifest.get("policy_version", "unknown")),
        prompt_version=TRADE_PLAN_PROMPT_VERSION,
        model_configuration=(model_reference if isinstance(model_reference, dict) else None),
        input_hash=stable_hash(
            {
                "request": request_payload,
                "run_input_hash": run.input_hash,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_hash": snapshot.payload_sha256,
                "optimizer_version": "trade-plan-grid-oos-v1",
            }
        ),
        active_trade_plan_key=active_key,
        created_at=now,
    )
    db.add(row)
    db.flush()
    operation = create_operation_run(
        db,
        user_id=row.user_id,
        run_type="TRADE_PLAN",
        resource_id=row.plan_id,
        trading_date=row.trading_date,
        decision_at=row.decision_at,
        input_hash=row.input_hash,
        manifest={
            "report_id": row.report_id,
            "research_run_id": row.run_id,
            "symbols": row.symbols,
        },
        created_at=row.created_at,
    )
    row.operation_run_id = operation.run_id
    AuditLogger(db).record(
        run.run_id,
        "TRADE_PLAN_SUBMITTED",
        "Trade Plan queued from immutable research report",
        details={
            "plan_id": row.plan_id,
            "report_id": report_id,
            "symbols": payload.symbols,
            "snapshot_ids": row.snapshot_ids,
            "input_hash": row.input_hash,
        },
    )
    _remember_idempotency(
        db,
        user_id=context.user.user_id,
        route=idempotency_route,
        fingerprint=fingerprint,
        resource_type="TRADE_PLAN",
        resource_id=row.plan_id,
    )
    try:
        db.commit()
        enqueue_trade_plan(row.plan_id)
    except IntegrityError as exc:
        db.rollback()
        replay = _find_idempotency(
            db,
            user_id=context.user.user_id,
            route=idempotency_route,
            fingerprint=fingerprint,
        )
        if replay is not None:
            winner_plan = db.get(TradePlanRow, replay.resource_id)
            if winner_plan is not None and winner_plan.user_id == context.user.user_id:
                response.status_code = status.HTTP_200_OK
                return TradePlanResponse.model_validate(winner_plan)
        winner = db.scalar(
            select(TradePlanRow).where(TradePlanRow.active_trade_plan_key == active_key)
        )
        if winner is None:
            raise HTTPException(status_code=409, detail="trade plan submission conflicted") from exc
        response.status_code = 200
        return TradePlanResponse.model_validate(winner)
    except Exception as exc:
        failed = db.get(TradePlanRow, row.plan_id)
        if failed is not None:
            failed.status = "FAILED"
            failed.active_trade_plan_key = None
            failed.error_message = safe_error_message(exc)
            failed.completed_at = datetime.now(UTC)
            db.commit()
        raise HTTPException(status_code=503, detail="trade plan queue unavailable") from exc
    return TradePlanResponse.model_validate(row)


@app.get(
    "/api/v1/reports/{report_id}/trade-plans",
    response_model=list[TradePlanResponse],
)
def report_trade_plans(report_id: str, db: DbSession, context: Current) -> list[TradePlanResponse]:
    report_row = db.get(ReportRow, report_id)
    if report_row is None:
        raise HTTPException(status_code=404, detail="report not found")
    run = db.get(JobRun, report_row.run_id)
    if run is None or not _owns(run, context):
        raise HTTPException(status_code=404, detail="report not found")
    rows = db.scalars(
        select(TradePlanRow)
        .where(TradePlanRow.report_id == report_id)
        .order_by(TradePlanRow.created_at.desc())
    ).all()
    return [TradePlanResponse.model_validate(item) for item in rows]


@app.get("/api/v1/trade-plans/{plan_id}", response_model=TradePlanResponse)
def trade_plan(plan_id: str, db: DbSession, context: Current) -> TradePlanResponse:
    row = db.get(TradePlanRow, plan_id)
    if row is None or (context.user.role != "ADMIN" and row.user_id != context.user.user_id):
        raise HTTPException(status_code=404, detail="trade plan not found")
    return TradePlanResponse.model_validate(row)


@app.get("/api/v1/snapshots", response_model=list[SnapshotResponse])
def snapshots(
    db: DbSession,
    context: Current,
    dataset: str = "backtest_bundle",
    include_non_executable: bool = False,
) -> list[SnapshotResponse]:
    if include_non_executable and context.user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="administrator required")
    statement = (
        select(SnapshotManifestRow)
        .join(JobRun, SnapshotManifestRow.run_id == JobRun.run_id)
        .where(
            SnapshotManifestRow.dataset == dataset,
            SnapshotManifestRow.status == "COMMITTED",
            JobRun.status.in_(("SUCCEEDED", "FUSED")),
        )
    )
    if context.user.role != "ADMIN":
        statement = statement.where(JobRun.user_id == context.user.user_id)
    rows = db.scalars(
        statement.order_by(
            SnapshotManifestRow.committed_at.desc(),
            SnapshotManifestRow.fetched_at.desc(),
        )
    ).all()
    if not include_non_executable:
        rows = [row for row in rows if int(row.details.get("executable_signal_count", 0)) >= 1]
    return [SnapshotResponse.model_validate(row) for row in rows]


@app.post("/api/v1/research/runs", response_model=RunResponse, status_code=202)
def submit_research(
    payload: ResearchRequest,
    response: Response,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> RunResponse:
    idempotency_route = "/api/v1/research/runs"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id,
        idempotency_route,
        idempotency_key,
        payload.model_dump(mode="json"),
    )
    replay = _find_idempotency(
        db,
        user_id=context.user.user_id,
        route=idempotency_route,
        fingerprint=fingerprint,
    )
    if replay is not None:
        existing_run = db.get(JobRun, replay.resource_id)
        if (
            replay.resource_type != "RESEARCH_RUN"
            or existing_run is None
            or existing_run.run_type != "DAILY"
            or not _owns(existing_run, context)
        ):
            raise HTTPException(status_code=409, detail="idempotent resource is unavailable")
        response.status_code = status.HTTP_200_OK
        return RunResponse.model_validate(existing_run)
    requested_date = payload.trading_date
    submitted_at = datetime.now(SHANGHAI)
    actual_research_date = _manual_research_date(requested_date, submitted_at)
    try:
        readiness_wait = _research_readiness_wait(actual_research_date, submitted_at)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="authoritative trading calendar unavailable for data readiness",
        ) from exc
    if payload.scope == "WATCHLIST":
        assets = UserAssetService(db).get(context.user.user_id)
        available_symbols = sorted(
            set(str(symbol) for symbol in assets.get("watchlist", []))
            | {
                str(position.get("symbol", "")).upper()
                for position in assets.get("positions", [])
                if position.get("symbol")
            }
        )
        if not available_symbols:
            raise HTTPException(status_code=422, detail="自选股与持仓为空，无法发起定向研究")
        if "symbols" in payload.model_fields_set:
            if not payload.symbols:
                raise HTTPException(status_code=422, detail="请至少选择一只自选股或持仓股票")
            outside = sorted(set(payload.symbols) - set(available_symbols))
            if outside:
                raise HTTPException(
                    status_code=422,
                    detail=f"所选股票不在当前自选股或持仓中：{'、'.join(outside)}",
                )
            target_symbols = sorted(payload.symbols)
        else:
            # Keep older clients compatible: omitting symbols still means all saved assets.
            target_symbols = available_symbols
    elif payload.scope == "CUSTOM":
        target_symbols = list(payload.symbols)
    else:
        target_symbols = []
    research_budget = {
        "total_budget": str(payload.total_budget) if payload.total_budget is not None else None,
        "per_symbol_budget": (
            str(payload.per_symbol_budget) if payload.per_symbol_budget is not None else None
        ),
        "max_stock_price": (
            str(payload.max_stock_price) if payload.max_stock_price is not None else None
        ),
    }
    target_count = _configured_portfolio_target_count()
    portfolio_requested = payload.scope == "MARKET" or len(target_symbols) >= target_count
    active_key = stable_hash(
        {
            "kind": "ACTIVE_DAILY_RESEARCH",
            "trigger_source": "MANUAL",
            "user_id": context.user.user_id,
            "trading_date": actual_research_date,
            "scope": payload.scope,
            "target_symbols": target_symbols,
            "research_budget": research_budget,
        }
    )
    existing = db.scalar(
        select(JobRun)
        .where(
            JobRun.active_research_key == active_key,
        )
        .order_by(JobRun.started_at.desc())
    )
    if existing is not None:
        if existing.status in {
            "PENDING",
            "RUNNING",
            "PROCESSING",
            "CANCEL_REQUESTED",
            "DATA_READINESS_WAITING",
        }:
            response.status_code = 200
            return RunResponse.model_validate(existing)
        existing.active_research_key = None
        db.commit()
    try:
        run_id = load_pipeline().start_run(actual_research_date)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="research pipeline unavailable") from exc
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=500, detail="pipeline did not create a run")
    run.user_id = context.user.user_id
    run.active_research_key = active_key
    run.status = "PENDING"
    tracked_symbols = target_symbols
    if payload.scope == "MARKET":
        tracked_symbols = list(
            db.scalars(
                select(CandidateRow.symbol)
                .join(JobRun, CandidateRow.run_id == JobRun.run_id)
                .where(
                    JobRun.user_id == context.user.user_id,
                    JobRun.run_type == "DAILY",
                    JobRun.status.in_(("SUCCEEDED", "FUSED")),
                    JobRun.trading_date < actual_research_date,
                )
                .order_by(JobRun.trading_date.desc(), CandidateRow.rank)
                .limit(80)
            ).all()
        )
    frozen_manifest = {
        **dict(run.manifest),
        "tracked_symbols": sorted(set(tracked_symbols)),
        "trigger_source": "MANUAL",
        "requested_date": requested_date.isoformat(),
        "actual_research_date": actual_research_date.isoformat(),
        "snapshot_mode": "SYSTEM_ENFORCED",
        "research_scope": payload.scope,
        "target_symbols": target_symbols,
        "research_budget": research_budget,
        "portfolio_requested": portfolio_requested,
        "research_only_reason": (
            None
            if portfolio_requested
            else f"定向研究标的少于 {target_count} 只，正式个股研究正常完成但不生成整体组合"
        ),
        "data_readiness_wait": readiness_wait,
    }
    run.manifest = frozen_manifest
    run.input_hash = stable_hash(frozen_manifest)
    if readiness_wait is not None:
        run.status = "DATA_READINESS_WAITING"
    AuditLogger(db).record(
        run_id,
        "RESEARCH_SUBMITTED",
        "Daily research queued by WebGUI",
        details={
            "user_id": context.user.user_id,
            "trigger_source": "MANUAL",
            "requested_date": requested_date.isoformat(),
            "actual_research_date": actual_research_date.isoformat(),
            "research_scope": payload.scope,
            "target_symbol_count": len(target_symbols),
            "portfolio_requested": portfolio_requested,
            "input_hash": run.input_hash,
        },
    )
    _remember_idempotency(
        db,
        user_id=context.user.user_id,
        route=idempotency_route,
        fingerprint=fingerprint,
        resource_type="RESEARCH_RUN",
        resource_id=run_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        replay = _find_idempotency(
            db,
            user_id=context.user.user_id,
            route=idempotency_route,
            fingerprint=fingerprint,
        )
        if replay is not None:
            winner_run = db.get(JobRun, replay.resource_id)
            if winner_run is not None and _owns(winner_run, context):
                response.status_code = status.HTTP_200_OK
                return RunResponse.model_validate(winner_run)
        winner = db.scalar(select(JobRun).where(JobRun.active_research_key == active_key))
        orphan = db.get(JobRun, run_id)
        if orphan is not None and (winner is None or orphan.run_id != winner.run_id):
            orphan.user_id = context.user.user_id
            orphan.active_research_key = None
            orphan.status = "FAILED"
            orphan.error_message = "deduplicated by a concurrent research submission"
            orphan.completed_at = datetime.now(UTC)
            AuditLogger(db).record(
                orphan.run_id,
                "RESEARCH_DEDUPLICATED",
                "Concurrent duplicate research submission was not queued",
                details={"winner_run_id": winner.run_id if winner else None},
            )
            db.commit()
        if winner is None:
            raise HTTPException(status_code=409, detail="research submission conflicted") from exc
        response.status_code = 200
        return RunResponse.model_validate(winner)
    try:
        if readiness_wait is not None:
            enqueue_research_at(
                run_id, datetime.fromisoformat(str(readiness_wait["next_retry_at"]))
            )
            AuditLogger(db).record(
                run_id,
                "DATA_READINESS_WAITING",
                "Research is waiting for all required benchmark data",
                details={
                    "trading_date": actual_research_date.isoformat(),
                    "next_retry_at": readiness_wait["next_retry_at"],
                    "deadline_at": readiness_wait["deadline_at"],
                },
            )
            db.commit()
        else:
            enqueue_research(run_id)
    except Exception as exc:
        run.status = "FAILED"
        run.active_research_key = None
        run.error_message = safe_error_message(exc)
        run.completed_at = datetime.now(UTC)
        AuditLogger(db).record(
            run_id,
            "RESEARCH_ENQUEUE_FAILED",
            "Daily research could not be queued",
            severity="ERROR",
            details={"error_type": type(exc).__name__},
        )
        db.commit()
        raise HTTPException(status_code=503, detail="research queue unavailable") from exc
    return RunResponse.model_validate(
        {
            **{column.name: getattr(run, column.name) for column in run.__table__.columns},
            "data_readiness_state": (
                "WAITING_FOR_BENCHMARKS" if run.status == "DATA_READINESS_WAITING" else None
            ),
            "next_retry_at": (dict(run.manifest).get("data_readiness_wait") or {}).get(
                "next_retry_at"
            ),
        }
    )


@app.get("/api/v1/research/settings", response_model=ResearchSettingsResponse)
def research_settings(db: DbSession, context: Current) -> ResearchSettingsResponse:
    settings = ResearchSettingsService(db).get(context.user.user_id)
    return ResearchSettingsResponse.model_validate(
        {**settings, "portfolio_target_count": _configured_portfolio_target_count()}
    )


@app.put("/api/v1/research/settings", response_model=ResearchSettingsResponse)
def update_research_settings(
    payload: ResearchSettingsRequest, db: DbSession, context: Writer
) -> ResearchSettingsResponse:
    reports = (
        [item.model_dump(mode="python") for item in payload.automatic_reports]
        if payload.automatic_reports is not None
        else None
    )
    settings = ResearchSettingsService(db).update(
        context.user.user_id,
        auto_enabled=payload.auto_enabled,
        automatic_reports=reports,
    )
    return ResearchSettingsResponse.model_validate(
        {**settings, "portfolio_target_count": _configured_portfolio_target_count()}
    )


@app.get("/api/v1/research/runs", response_model=list[ResearchRunResponse])
def research_runs(
    db: DbSession,
    context: Current,
    limit: int = Query(default=5, ge=1, le=50),
    trading_date: date | None = None,
    mine: bool = False,
    published: bool = False,
) -> list[ResearchRunResponse]:
    statement = select(JobRun).where(JobRun.run_type == "DAILY")
    if mine or context.user.role != "ADMIN":
        statement = statement.where(JobRun.user_id == context.user.user_id)
    if trading_date is not None:
        statement = statement.where(JobRun.trading_date == trading_date)
    if published:
        statement = statement.where(JobRun.status.in_(("SUCCEEDED", "FUSED"))).order_by(
            JobRun.completed_at.desc(), JobRun.started_at.desc(), JobRun.run_id.desc()
        )
    else:
        statement = statement.order_by(JobRun.started_at.desc(), JobRun.run_id.desc())
    rows = db.scalars(statement.limit(limit)).all()
    return [_research_run_response(db, row) for row in rows]


@app.get("/api/v1/research/runs/{run_id}", response_model=ResearchRunResponse)
def research_run(run_id: str, db: DbSession, context: Current) -> ResearchRunResponse:
    """Return pollable research progress without exposing another user's run."""

    row = db.get(JobRun, run_id)
    if row is None or row.run_type != "DAILY" or not _owns(row, context):
        raise HTTPException(status_code=404, detail="research run not found")
    return _research_run_response(db, row)


@app.post(
    "/api/v1/research/runs/{run_id}/cancel",
    response_model=ResearchRunResponse,
)
def cancel_research_run(
    run_id: str,
    db: DbSession,
    context: Writer,
) -> ResearchRunResponse:
    row = db.scalar(
        select(JobRun)
        .where(
            JobRun.run_id == run_id,
            JobRun.run_type == "DAILY",
            JobRun.user_id == context.user.user_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="research run not found")

    normalized = row.status.upper()
    if normalized in {"SUCCEEDED", "FAILED", "FUSED", "CANCELLED", "CANCEL_REQUESTED"}:
        raise HTTPException(
            status_code=409,
            detail=f"research run cannot be cancelled from status {normalized}",
        )

    now = datetime.now(UTC)
    AuditLogger(db).record(
        run_id,
        "RESEARCH_CANCEL_REQUESTED",
        "Daily research stop requested by its owner",
        details={"user_id": context.user.user_id, "previous_status": normalized},
    )
    if normalized in {"PENDING", "QUEUED", "DATA_READINESS_WAITING"}:
        row.status = "CANCELLED"
        row.active_research_key = None
        row.error_message = None
        row.completed_at = now
        AuditLogger(db).record(
            run_id,
            "RESEARCH_CANCELLED",
            "Queued daily research cancelled before worker execution",
            details={
                "boundary": (
                    "queued" if normalized != "DATA_READINESS_WAITING" else "data_readiness_wait"
                )
            },
        )
    elif normalized in {"RUNNING", "PROCESSING"}:
        row.status = "CANCEL_REQUESTED"
    else:
        raise HTTPException(
            status_code=409,
            detail=f"research run cannot be cancelled from status {normalized}",
        )
    db.commit()
    db.refresh(row)
    return _research_run_response(db, row)


@app.get("/api/v1/runs", response_model=list[RunListResponse])
def runs(
    db: DbSession,
    context: Current,
    run_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[RunListResponse]:
    statement = select(JobRun)
    if context.user.role != "ADMIN":
        statement = statement.where(JobRun.user_id == context.user.user_id)
    if run_type:
        statement = statement.where(JobRun.run_type == run_type.upper())
    rows = db.scalars(statement.order_by(JobRun.started_at.desc()).limit(limit)).all()
    return [RunListResponse.model_validate(row) for row in rows]


@app.get("/api/v1/runs/activity", response_model=RunActivityResponse)
def run_activity(
    db: DbSession,
    context: Current,
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    run_type: str | None = Query(default=None, alias="type"),
    status_filter: str | None = Query(default=None, alias="status"),
) -> RunActivityResponse:
    statement = select(JobRun).where(JobRun.user_id == context.user.user_id)
    allowed_types = {"DAILY", "RESEARCH", "BACKTEST", "TRADE_PLAN", "EXIT_ADVICE"}
    if run_type:
        requested = {item.strip().upper() for item in run_type.split(",")}
        statement = statement.where(JobRun.run_type.in_(requested & allowed_types))
    if status_filter:
        statement = statement.where(JobRun.status == status_filter.upper())
    if cursor:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode() + b"===").decode()
            stamp_value, cursor_run_id = raw.split("|", 1)
            stamp = datetime.fromisoformat(stamp_value)
        except (ValueError, UnicodeDecodeError, TypeError):
            raise HTTPException(status_code=422, detail="invalid activity cursor") from None
        statement = statement.where(
            (JobRun.started_at < stamp)
            | ((JobRun.started_at == stamp) & (JobRun.run_id < cursor_run_id))
        )
    rows = list(
        db.scalars(
            statement.order_by(JobRun.started_at.desc(), JobRun.run_id.desc()).limit(limit + 1)
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items: list[RunActivityItem] = []
    for row in rows:
        resource_type = cast(
            Literal["RESEARCH", "BACKTEST", "TRADE_PLAN", "EXIT_ADVICE"],
            {
                "DAILY": "RESEARCH",
                "RESEARCH": "RESEARCH",
                "BACKTEST": "BACKTEST",
                "TRADE_PLAN": "TRADE_PLAN",
                "EXIT_ADVICE": "EXIT_ADVICE",
            }.get(row.run_type, "RESEARCH"),
        )
        resource_id = str((row.manifest or {}).get("resource_id") or "") or None
        resource_url = None
        title = row.run_type.replace("_", " ")
        symbol = None
        if resource_type == "TRADE_PLAN" and resource_id:
            plan = db.get(TradePlanRow, resource_id)
            if plan:
                symbol = plan.symbols[0] if plan.symbols else None
                resource_url = (
                    f"/reports?date={plan.trading_date.isoformat()}&run_id={plan.run_id}"
                    + (f"&symbol={symbol}" if symbol else "")
                )
                title = "买入方案"
        elif resource_type == "EXIT_ADVICE" and resource_id:
            advice = db.get(ExitAdviceRow, resource_id)
            if advice:
                symbol = advice.symbol
                resource_url = f"/exit-advice?advice_id={advice.advice_id}"
                title = "卖出建议"
        elif resource_type == "BACKTEST":
            backtest = db.scalar(select(BacktestRun).where(BacktestRun.run_id == row.run_id))
            if backtest:
                resource_id = backtest.backtest_id
                resource_url = f"/backtest?backtest_id={backtest.backtest_id}"
                title = backtest.name
        else:
            report = db.scalar(select(ReportRow).where(ReportRow.run_id == row.run_id))
            if report:
                resource_id = report.report_id
                resource_url = (
                    f"/reports?date={report.trading_date.isoformat()}&run_id={row.run_id}"
                )
                title = "研究报告"
        items.append(
            RunActivityItem(
                **RunResponse.model_validate(row).model_dump(),
                user_id=row.user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_url=resource_url,
                title=title,
                symbol=symbol,
            )
        )
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = (
            base64.urlsafe_b64encode(f"{last.started_at.isoformat()}|{last.run_id}".encode())
            .decode()
            .rstrip("=")
        )
    return RunActivityResponse(items=items, next_cursor=next_cursor)


@app.get("/api/v1/runs/{run_id}", response_model=RunResponse)
def run(run_id: str, db: DbSession, context: Current) -> RunResponse:
    row = QueryRepository(db).run(run_id)
    if row is None or not _owns(row, context):
        raise HTTPException(status_code=404, detail="run not found")
    return RunResponse.model_validate(row)


@app.get("/api/v1/runs/{run_id}/audit", response_model=list[AuditEventResponse])
def run_audit(run_id: str, db: DbSession, context: Current) -> list[AuditEventResponse]:
    repository = QueryRepository(db)
    row = repository.run(run_id)
    if row is None or not _owns(row, context):
        raise HTTPException(status_code=404, detail="run not found")
    return [AuditEventResponse.model_validate(item) for item in repository.audit(run_id)]


@app.post(
    "/api/v1/backtests", response_model=BacktestResponse, status_code=status.HTTP_202_ACCEPTED
)
def submit_backtest(
    request: BacktestRequest,
    response: Response,
    db: DbSession,
    context: Writer,
    idempotency_key: IdempotencyKey = None,
) -> BacktestResponse:
    idempotency_route = "/api/v1/backtests"
    fingerprint = _idempotency_fingerprint(
        context.user.user_id,
        idempotency_route,
        idempotency_key,
        request.model_dump(mode="json"),
    )
    replay = _find_idempotency(
        db,
        user_id=context.user.user_id,
        route=idempotency_route,
        fingerprint=fingerprint,
    )
    if replay is not None:
        existing_backtest = db.get(BacktestRun, replay.resource_id)
        if (
            replay.resource_type != "BACKTEST"
            or existing_backtest is None
            or existing_backtest.user_id != context.user.user_id
        ):
            raise HTTPException(status_code=409, detail="idempotent resource is unavailable")
        response.status_code = status.HTTP_200_OK
        return _backtest_response(db, existing_backtest)
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    if len(request.snapshot_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail="select exactly one cumulative backtest snapshot",
        )
    manifest_statement = (
        select(SnapshotManifestRow)
        .join(JobRun, SnapshotManifestRow.run_id == JobRun.run_id)
        .where(
            SnapshotManifestRow.snapshot_id.in_(request.snapshot_ids),
            SnapshotManifestRow.dataset == "backtest_bundle",
            SnapshotManifestRow.status == "COMMITTED",
            JobRun.status.in_(("SUCCEEDED", "FUSED")),
        )
    )
    if context.user.role != "ADMIN":
        manifest_statement = manifest_statement.where(JobRun.user_id == context.user.user_id)
    manifests = db.scalars(manifest_statement).all()
    if len(manifests) != len(request.snapshot_ids):
        raise HTTPException(status_code=422, detail="committed snapshot is unavailable")
    selected_manifest = manifests[0]
    try:
        calendar_start = date.fromisoformat(str(selected_manifest.details["calendar_start"]))
        calendar_end = date.fromisoformat(str(selected_manifest.details["calendar_end"]))
        executable_signals = int(selected_manifest.details["executable_signal_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="snapshot lacks calendar metadata") from exc
    if (calendar_start - request.start_date).days > 7 or (request.end_date - calendar_end).days > 7:
        raise HTTPException(
            status_code=422,
            detail=(
                "requested range is outside snapshot calendar "
                f"[{calendar_start.isoformat()}, {calendar_end.isoformat()}]"
            ),
        )
    if executable_signals < 1:
        raise HTTPException(
            status_code=422,
            detail="snapshot has no PIT signal with a following execution session",
        )
    try:
        file_hashes = {
            item.snapshot_id: str(item.details["parquet_file_sha256"]) for item in manifests
        }
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="snapshot lacks a verified file hash") from exc
    requested_config = dict(request.config)
    runtime_settings = get_effective_settings()
    system_configuration = SystemConfigurationService().resolve(db).manifest_reference()
    executor_config = {
        "artifact_root": str(runtime_settings.lake_root.parent / "artifacts"),
        "snapshot_file_hashes": file_hashes,
        "requested_start_date": request.start_date.isoformat(),
        "requested_end_date": request.end_date.isoformat(),
        "initial_capital": requested_config.get("initial_capital", 1_000_000),
        "benchmark": requested_config.get("benchmark", "000300.SH"),
    }
    input_hash = stable_hash(
        {"user_id": context.user.user_id, "request": request, "executor": executor_config}
    )
    existing = db.scalar(
        select(BacktestRun).where(
            BacktestRun.user_id == context.user.user_id, BacktestRun.input_hash == input_hash
        )
    )
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return _backtest_response(db, existing)
    now = datetime.now(UTC)
    run_id = str(uuid4())
    job = JobRun(
        run_id=run_id,
        user_id=context.user.user_id,
        run_type="BACKTEST",
        trading_date=request.end_date,
        decision_at=now,
        status="PENDING",
        idempotency_key=input_hash,
        manifest={
            "snapshot_ids": request.snapshot_ids,
            "config": requested_config,
            "system_configuration": system_configuration,
        },
        input_hash=input_hash,
        started_at=now,
    )
    backtest = BacktestRun(
        run_id=run_id,
        user_id=context.user.user_id,
        name=request.name,
        status="PENDING",
        start_date=request.start_date,
        end_date=request.end_date,
        config=executor_config,
        snapshot_ids=request.snapshot_ids,
        input_hash=input_hash,
        created_at=now,
    )
    db.add(job)
    db.flush()
    db.add(backtest)
    db.flush()
    AuditLogger(db).record(
        run_id,
        "BACKTEST_SUBMITTED",
        "Backtest accepted for asynchronous execution",
        details={
            "backtest_id": backtest.backtest_id,
            "input_hash": input_hash,
            "user_id": context.user.user_id,
            "system_configuration": system_configuration,
        },
    )
    _remember_idempotency(
        db,
        user_id=context.user.user_id,
        route=idempotency_route,
        fingerprint=fingerprint,
        resource_type="BACKTEST",
        resource_id=backtest.backtest_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        replay = _find_idempotency(
            db,
            user_id=context.user.user_id,
            route=idempotency_route,
            fingerprint=fingerprint,
        )
        if replay is not None:
            winner_backtest = db.get(BacktestRun, replay.resource_id)
            if winner_backtest is not None and winner_backtest.user_id == context.user.user_id:
                response.status_code = status.HTTP_200_OK
                return _backtest_response(db, winner_backtest)
        raise HTTPException(status_code=409, detail="backtest submission conflicted") from exc
    try:
        enqueue_backtest(backtest.backtest_id)
    except Exception as exc:
        failed_at = datetime.now(UTC)
        job.status = "FAILED"
        job.error_message = safe_error_message(exc)
        job.completed_at = failed_at
        backtest.status = "FAILED"
        backtest.completed_at = failed_at
        AuditLogger(db).record(
            run_id,
            "BACKTEST_ENQUEUE_FAILED",
            "Backtest could not be queued",
            severity="ERROR",
            details={"backtest_id": backtest.backtest_id, "error_type": type(exc).__name__},
        )
        db.commit()
        raise HTTPException(status_code=503, detail="backtest queue unavailable") from exc
    return _backtest_response(db, backtest)


@app.get("/api/v1/backtests", response_model=list[BacktestResponse])
def backtests(
    db: DbSession, context: Current, limit: int = Query(default=100, ge=1, le=500)
) -> list[BacktestResponse]:
    statement = select(BacktestRun)
    if context.user.role != "ADMIN":
        statement = statement.where(BacktestRun.user_id == context.user.user_id)
    rows = db.scalars(statement.order_by(BacktestRun.created_at.desc()).limit(limit)).all()
    return [_backtest_response(db, row) for row in rows]


@app.get("/api/v1/backtests/{backtest_id}", response_model=BacktestResponse)
def backtest(backtest_id: str, db: DbSession, context: Current) -> BacktestResponse:
    row = QueryRepository(db).backtest(backtest_id)
    if row is None or (context.user.role != "ADMIN" and row.user_id != context.user.user_id):
        raise HTTPException(status_code=404, detail="backtest not found")
    return _backtest_response(db, row)


@app.post(
    "/api/v1/backtests/{backtest_id}/retry",
    response_model=BacktestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_backtest(backtest_id: str, db: DbSession, context: Writer) -> BacktestResponse:
    row = db.get(BacktestRun, backtest_id)
    if row is None or row.user_id != context.user.user_id:
        raise HTTPException(status_code=404, detail="backtest not found")
    if row.status != "FAILED":
        raise HTTPException(status_code=409, detail="only failed backtests can be retried")
    job = db.get(JobRun, row.run_id) if row.run_id else None
    if job is None:
        raise HTTPException(status_code=409, detail="backtest job is unavailable")
    manifests = db.scalars(
        select(SnapshotManifestRow).where(
            SnapshotManifestRow.snapshot_id.in_(row.snapshot_ids),
            SnapshotManifestRow.status == "COMMITTED",
        )
    ).all()
    by_id = {item.snapshot_id: item for item in manifests}
    if set(by_id) != set(row.snapshot_ids):
        raise HTTPException(status_code=409, detail="committed snapshot is unavailable")
    expected_hashes = dict(row.config.get("snapshot_file_hashes", {}))
    actual_hashes = {
        snapshot_id: str(by_id[snapshot_id].details.get("parquet_file_sha256", ""))
        for snapshot_id in row.snapshot_ids
    }
    if expected_hashes != actual_hashes or any(
        len(value) != 64 for value in actual_hashes.values()
    ):
        raise HTTPException(status_code=409, detail="snapshot hash metadata changed")
    try:
        read_backtest_bundle(
            {snapshot_id: by_id[snapshot_id].parquet_uri for snapshot_id in row.snapshot_ids},
            expected_hashes,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="snapshot hash validation failed") from exc
    row.retry_count += 1
    row.status = "PENDING"
    row.metrics = None
    row.artifacts = None
    row.output_hash = None
    row.completed_at = None
    job.status = "PENDING"
    job.error_message = None
    job.output_hash = None
    job.completed_at = None
    AuditLogger(db).record(
        job.run_id,
        "BACKTEST_RETRY_REQUESTED",
        "Failed backtest was validated and queued again",
        details={"backtest_id": backtest_id, "retry_count": row.retry_count},
    )
    db.commit()
    try:
        enqueue_backtest(backtest_id)
    except Exception as exc:
        failed_at = datetime.now(UTC)
        row.status = "FAILED"
        row.completed_at = failed_at
        job.status = "FAILED"
        job.error_message = safe_error_message(exc)
        job.completed_at = failed_at
        AuditLogger(db).record(
            job.run_id,
            "BACKTEST_RETRY_ENQUEUE_FAILED",
            "Validated retry could not be queued",
            severity="ERROR",
            details={"backtest_id": backtest_id, "retry_count": row.retry_count},
        )
        db.commit()
        raise HTTPException(status_code=503, detail="backtest queue unavailable") from exc
    return _backtest_response(db, row)


@app.get("/api/v1/market/quotes/{symbol}", response_model=QuoteResponse)
def market_quote(symbol: str, _: Current, refresh: bool = False) -> QuoteResponse:
    try:
        row = get_market_data_service().quote(symbol, force_refresh=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="market quote unavailable") from exc
    return QuoteResponse.model_validate(row)


@app.get("/api/v1/market/quotes", response_model=list[QuoteResponse])
def market_quotes(symbols: str, _: Current, refresh: bool = False) -> list[QuoteResponse]:
    try:
        rows = get_market_data_service().quotes(symbols.split(","), force_refresh=refresh)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="market quotes unavailable") from exc
    return [QuoteResponse.model_validate(row) for row in rows]


@app.get("/api/v1/market/klines/{symbol}", response_model=KlineResponse)
@app.get("/api/v1/market/kline/{symbol}", response_model=KlineResponse, include_in_schema=False)
def market_klines(
    symbol: str,
    _: Current,
    period: str = "day",
    limit: int = Query(default=300, ge=1, le=5000),
    adjust: str = "hfq",
    start: datetime | None = None,
    end: datetime | None = None,
    refresh: bool = False,
) -> KlineResponse:
    if adjust.casefold() != "hfq":
        raise HTTPException(status_code=422, detail="only hfq adjustment is supported")
    try:
        return KlineResponse.model_validate(
            get_market_data_service().klines(
                symbol,
                period,
                limit=limit,
                start=start,
                end=end,
                force_refresh=refresh,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="market kline unavailable") from exc


@app.post("/api/v1/market/prefetch", response_model=MarketPrefetchResponse)
def market_prefetch(request: MarketPrefetchRequest, _: Writer) -> MarketPrefetchResponse:
    try:
        payload = get_market_data_service().prefetch(
            request.symbols,
            periods=request.periods,
            limit=request.limit,
            include_quotes=request.include_quotes,
        )
        return MarketPrefetchResponse.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/market/status")
def market_status(_: Current) -> dict[str, Any]:
    return {
        **get_market_data_service().status(),
        "market_session": _market_session_status().model_dump(mode="json"),
    }


@app.get("/api/v1/search/financial", response_model=FinancialSearchResponse)
def financial_search(
    service: SearchService,
    db: DbSession,
    context: Current,
    q: str = Query(min_length=1, max_length=256),
) -> FinancialSearchResponse:
    if not service.allow_user_request(context.user.user_id):
        raise HTTPException(
            status_code=429,
            detail="financial search rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    try:
        if isinstance(service, FinancialSearchService):
            return service.search(q, db)
        return service.search(q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="financial search timed out") from exc
    except FinancialSearchBusyError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "1"},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="financial search unavailable") from exc


@app.get("/api/v1/search/status", response_model=FinancialSearchStatus)
def financial_search_status(
    service: SearchService, db: DbSession, _: Current
) -> FinancialSearchStatus:
    if isinstance(service, FinancialSearchService):
        return service.status(db)
    return service.status()
