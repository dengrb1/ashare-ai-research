from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ashare_ai import __version__
from ashare_ai.api.auth import (
    AuthContext,
    authenticate,
    bootstrap_admin,
    clear_auth_cookies,
    create_session,
    hash_password,
    invalidate_user_sessions,
    require_admin,
    revoke_session,
)
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.api.schemas import (
    AuditEventResponse,
    BacktestRequest,
    BacktestResponse,
    CandidateResponse,
    HealthResponse,
    KlineResponse,
    LoginRequest,
    MarketPrefetchRequest,
    MarketPrefetchResponse,
    PasswordResetRequest,
    PortfolioResponse,
    QuoteResponse,
    ReportBodyResponse,
    ReportResponse,
    ResearchRequest,
    RunListResponse,
    RunResponse,
    ScoreResponse,
    SnapshotResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.market.service import get_market_data_service
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.orchestration.backtest_jobs import enqueue_backtest
from ashare_ai.orchestration.daily import load_pipeline
from ashare_ai.orchestration.research_jobs import enqueue_research
from ashare_ai.search.service import (
    FinancialSearchBusyError,
    FinancialSearchResponse,
    FinancialSearchService,
    FinancialSearchStatus,
    get_financial_search_service,
)
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    BacktestRun,
    CandidateRow,
    JobRun,
    ReportRow,
    SnapshotManifestRow,
    UserAccount,
)
from ashare_ai.storage.objects import LocalObjectStore, ObjectStore, S3ObjectStore
from ashare_ai.storage.repositories import QueryRepository
from ashare_ai.trading.default_rules import ensure_builtin_trading_rules


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    with SessionLocal() as session:
        bootstrap_admin(session)
        ensure_builtin_trading_rules(session)
        session.commit()
    yield


app = FastAPI(
    title="A-share AI Research API",
    version=__version__,
    description="Authenticated research, live market data and asynchronous fixed-snapshot jobs.",
    lifespan=lifespan,
)
DbSession = Annotated[Session, Depends(get_db)]
Current = Annotated[AuthContext, Depends(get_auth_context)]
Writer = Annotated[AuthContext, Depends(get_write_context)]
SearchService = Annotated[FinancialSearchService, Depends(get_financial_search_service)]


def _admin(context: AuthContext) -> None:
    require_admin(context)


def _owns(run: JobRun, context: AuthContext) -> bool:
    return context.user.role == "ADMIN" or run.user_id == context.user.user_id


def _result_access(context: AuthContext) -> dict[str, Any]:
    return {
        "user_id": context.user.user_id,
        "include_all_users": context.user.role == "ADMIN",
    }


def _backtest_response(db: Session, row: BacktestRun) -> BacktestResponse:
    job = db.get(JobRun, row.run_id) if row.run_id else None
    return BacktestResponse.model_validate(
        {
            **{column.name: getattr(row, column.name) for column in row.__table__.columns},
            "error_message": job.error_message if job else None,
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
    user = authenticate(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid username or password")
    create_session(db, user, response, request)
    return UserResponse.model_validate(user)


@app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, db: DbSession, context: Writer) -> None:
    revoke_session(db, context)
    clear_auth_cookies(response)


@app.get("/api/v1/auth/me", response_model=UserResponse)
def me(context: Current) -> UserResponse:
    return UserResponse.model_validate(context.user)


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
    return [
        CandidateResponse.model_validate(row)
        for row in QueryRepository(db).candidates(
            trading_date, run_id=run_id, **_result_access(context)
        )
    ]


@app.get("/api/v1/portfolios/{trading_date}", response_model=PortfolioResponse)
def portfolio(
    trading_date: date, db: DbSession, context: Current, run_id: str | None = None
) -> PortfolioResponse:
    row = QueryRepository(db).portfolio(trading_date, run_id=run_id, **_result_access(context))
    if row is None:
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
    settings = get_settings()
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
    payload: ResearchRequest, response: Response, db: DbSession, context: Writer
) -> RunResponse:
    active_key = stable_hash(
        {
            "kind": "ACTIVE_DAILY_RESEARCH",
            "user_id": context.user.user_id,
            "trading_date": payload.trading_date,
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
        if existing.status in {"PENDING", "RUNNING"}:
            response.status_code = 200
            return RunResponse.model_validate(existing)
        existing.active_research_key = None
        db.commit()
    try:
        run_id = load_pipeline().start_run(payload.trading_date)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"research pipeline unavailable: {exc}"
        ) from exc
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=500, detail="pipeline did not create a run")
    run.user_id = context.user.user_id
    run.active_research_key = active_key
    run.status = "PENDING"
    tracked_symbols = db.scalars(
        select(CandidateRow.symbol)
        .join(JobRun, CandidateRow.run_id == JobRun.run_id)
        .where(
            JobRun.user_id == context.user.user_id,
            JobRun.run_type == "DAILY",
            JobRun.status.in_(("SUCCEEDED", "FUSED")),
            JobRun.trading_date < payload.trading_date,
        )
        .order_by(JobRun.trading_date.desc(), CandidateRow.rank)
        .limit(80)
    ).all()
    run.manifest = {
        **dict(run.manifest),
        "tracked_symbols": sorted(set(tracked_symbols)),
    }
    AuditLogger(db).record(
        run_id,
        "RESEARCH_SUBMITTED",
        "Daily research queued by WebGUI",
        details={"user_id": context.user.user_id},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
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
        enqueue_research(run_id)
    except Exception as exc:
        run.status = "FAILED"
        run.active_research_key = None
        run.error_message = str(exc)
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
    return RunResponse.model_validate(run)


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
    request: BacktestRequest, response: Response, db: DbSession, context: Writer
) -> BacktestResponse:
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
    executor_config = {
        "artifact_root": str(get_settings().lake_root.parent / "artifacts"),
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
        manifest={"snapshot_ids": request.snapshot_ids, "config": requested_config},
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
        },
    )
    db.commit()
    try:
        enqueue_backtest(backtest.backtest_id)
    except Exception as exc:
        failed_at = datetime.now(UTC)
        job.status = "FAILED"
        job.error_message = str(exc)
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


@app.get("/api/v1/market/quotes", response_model=list[QuoteResponse])
def market_quotes(symbols: str, _: Current) -> list[QuoteResponse]:
    try:
        rows = get_market_data_service().quotes(symbols.split(","))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/market/prefetch", response_model=MarketPrefetchResponse)
def market_prefetch(request: MarketPrefetchRequest, _: Writer) -> MarketPrefetchResponse:
    try:
        payload = get_market_data_service().prefetch(
            request.symbols,
            periods=request.periods,
            limit=request.limit,
        )
        return MarketPrefetchResponse.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/market/status")
def market_status(_: Current) -> dict[str, Any]:
    return get_market_data_service().status()


@app.get("/api/v1/search/financial", response_model=FinancialSearchResponse)
def financial_search(
    service: SearchService,
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
        return service.search(q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except FinancialSearchBusyError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "1"},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/search/status", response_model=FinancialSearchStatus)
def financial_search_status(service: SearchService, _: Current) -> FinancialSearchStatus:
    return service.status()
