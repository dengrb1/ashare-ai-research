from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from importlib import import_module
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.core.security import safe_error_message
from ashare_ai.core.time import SHANGHAI
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.orchestration.research_jobs import enqueue_research
from ashare_ai.orchestration.research_settings import ResearchSettingsService
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    AutomaticResearchReportConfig,
    JobRun,
    UserAccount,
    UserAssetState,
)

MARKET_OPEN = time(9, 0)
AUTO_START = time(15, 5)
AUTO_RETRY_WINDOW = timedelta(hours=2)


class TradingCalendarProvider(Protocol):
    def sessions(self, start_date: date, end_date: date) -> tuple[date, ...]: ...


class DataReadinessProbe(Protocol):
    def ready(self, trading_date: date, checked_at: datetime) -> bool: ...


class FreeExchangeCalendar:
    """Free exchange-session calendar exposed by AKShare's Sina calendar adapter."""

    def sessions(self, start_date: date, end_date: date) -> tuple[date, ...]:
        sdk = import_module("akshare")
        frame = sdk.tool_trade_date_hist_sina()
        values: set[date] = set()
        for row in frame.to_dict(orient="records"):
            raw = row.get("trade_date", row.get("日期"))
            parsed = _date_value(raw)
            if parsed is not None and start_date <= parsed <= end_date:
                values.add(parsed)
        return tuple(sorted(values))


class AKShareDataReadiness:
    def ready(self, trading_date: date, checked_at: datetime) -> bool:
        del checked_at
        from ashare_ai.orchestration.akshare_bundle import AKShareCanonicalProvider

        rows = AKShareCanonicalProvider().benchmark_bars(
            "000300", trading_date - timedelta(days=10), trading_date
        )
        available = sorted(
            parsed
            for row in rows
            if (parsed := _date_value(row.get("日期", row.get("date")))) is not None
        )
        return bool(available) and available[-1] == trading_date


def resolve_manual_research_date(
    *,
    requested_date: date,
    now: datetime,
    sessions: tuple[date, ...],
    data_ready: Callable[[date], bool],
) -> date:
    current = _aware_shanghai(now)
    if requested_date > current.date():
        raise RuntimeError("future research dates are unavailable")
    ordered = tuple(sorted(set(sessions)))
    current_is_session = current.date() in ordered

    if current_is_session and MARKET_OPEN <= current.time() < AUTO_START:
        raise RuntimeError("live canonical data is unsafe during the trading session")

    if current_is_session and current.time() >= AUTO_START:
        # Once today's market has opened, the live spot snapshot can no longer be
        # used to reconstruct a prior session without look-ahead contamination.
        if requested_date != current.date():
            raise RuntimeError("historical live reconstruction is unavailable after market open")
        candidate = current.date()
    else:
        completed = [value for value in ordered if value < current.date()]
        if not completed:
            raise RuntimeError("no completed trading session is available")
        candidate = completed[-1]
        if requested_date < candidate:
            raise RuntimeError("only the latest uncontaminated session can use live data")

    if not data_ready(candidate):
        raise RuntimeError("the selected trading session is not ready")
    return candidate


def auto_dispatch_state(
    *,
    now: datetime,
    sessions: tuple[date, ...],
    data_ready: bool,
) -> str:
    current = _aware_shanghai(now)
    if current.date() not in sessions:
        return "NON_TRADING_DAY"
    start = datetime.combine(current.date(), AUTO_START, tzinfo=SHANGHAI)
    if current < start:
        return "BEFORE_WINDOW"
    if current > start + AUTO_RETRY_WINDOW:
        return "RETRY_EXPIRED"
    return "READY" if data_ready else "WAITING_FOR_DATA"


def dispatch_auto_research(
    *,
    now: datetime | None = None,
    calendar: TradingCalendarProvider | None = None,
    readiness: DataReadinessProbe | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    pipeline_factory: Callable[[], Any] | None = None,
    enqueue: Callable[[str], None] = enqueue_research,
) -> dict[str, Any]:
    current = _aware_shanghai(now or datetime.now(SHANGHAI))
    calendar_provider = calendar or FreeExchangeCalendar()
    readiness_probe = readiness or AKShareDataReadiness()
    try:
        sessions = calendar_provider.sessions(
            current.date() - timedelta(days=10), current.date() + timedelta(days=1)
        )
    except Exception as exc:
        return {"state": "CALENDAR_UNAVAILABLE", "error_type": type(exc).__name__, "queued": []}
    try:
        is_ready = readiness_probe.ready(current.date(), current)
    except Exception:
        is_ready = False
    state = auto_dispatch_state(now=current, sessions=sessions, data_ready=is_ready)
    if state != "READY":
        return {"state": state, "queued": []}

    if pipeline_factory is None:
        from ashare_ai.orchestration.daily import load_pipeline

        pipeline_factory = load_pipeline
    pipeline = pipeline_factory()
    queued: list[str] = []
    skipped: list[dict[str, str]] = []
    with session_factory() as session:
        user_ids = list(
            session.scalars(
                select(AutomaticResearchReportConfig.user_id)
                .join(UserAccount, UserAccount.user_id == AutomaticResearchReportConfig.user_id)
                .where(
                    AutomaticResearchReportConfig.enabled.is_(True),
                    UserAccount.enabled.is_(True),
                )
                .distinct()
            ).all()
        )
        user_settings = {
            user_id: ResearchSettingsService(session).get(user_id) for user_id in user_ids
        }
    for user_id in user_ids:
        reports = user_settings[user_id]["automatic_reports"]
        for report in reports:
            if not report["enabled"]:
                continue
            try:
                run_id = _submit_auto_for_user(
                    user_id=user_id,
                    trading_date=current.date(),
                    report=report,
                    pipeline=pipeline,
                    session_factory=session_factory,
                    enqueue=enqueue,
                )
                if run_id is not None:
                    queued.append(run_id)
                elif report["scope"] == "WATCHLIST":
                    skipped.append(
                        {"user_id": user_id, "slot": report["slot"], "reason": "EMPTY_SCOPE"}
                    )
            except Exception as exc:
                skipped.append(
                    {
                        "user_id": user_id,
                        "slot": report["slot"],
                        "reason": type(exc).__name__,
                    }
                )
    return {
        "state": "READY",
        "queued": queued,
        "enabled_user_count": len(user_ids),
        "skipped": skipped,
    }


def _submit_auto_for_user(
    *,
    user_id: str,
    trading_date: date,
    report: dict[str, Any],
    pipeline: Any,
    session_factory: Callable[[], Session],
    enqueue: Callable[[str], None],
) -> str | None:
    slot = str(report["slot"])
    scope = str(report["scope"])
    target_symbols = list(report.get("symbols") or [])
    if scope == "WATCHLIST":
        with session_factory() as session:
            assets = session.get(UserAssetState, user_id)
            if assets is None:
                return None
            target_symbols = sorted(
                set(str(symbol) for symbol in assets.watchlist)
                | {
                    str(position.get("symbol", "")).upper()
                    for position in assets.positions
                    if position.get("symbol")
                }
            )
        if not target_symbols:
            return None
    elif scope == "MARKET":
        target_symbols = []
    budget = {
        "total_budget": str(report["total_budget"]),
        "per_symbol_budget": str(report["per_symbol_budget"]),
        "max_stock_price": (
            str(report["max_stock_price"]) if report.get("max_stock_price") is not None else None
        ),
    }
    frozen_config = {
        "slot": slot,
        "scope": scope,
        "symbols": target_symbols,
        "research_budget": budget,
        "config_version": int(report.get("config_version", 1)),
    }
    idempotency_key = _automatic_run_key(
        kind="AUTO_DAILY_RESEARCH",
        user_id=user_id,
        trading_date=trading_date,
        slot=slot,
    )
    with session_factory() as session:
        existing = session.scalar(
            select(JobRun).where(JobRun.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return None
    run_id = str(pipeline.start_run(trading_date))
    with session_factory() as session:
        run = session.get(JobRun, run_id)
        if run is None:
            raise RuntimeError("pipeline did not persist automatic research run")
        active_key = _automatic_run_key(
            kind="ACTIVE_DAILY_RESEARCH",
            user_id=user_id,
            trading_date=trading_date,
            slot=slot,
            trigger_source="AUTO",
        )
        target_count = _configured_portfolio_target_count()
        portfolio_requested = scope == "MARKET" or len(target_symbols) >= target_count
        manifest = {
            **dict(run.manifest),
            "trigger_source": "AUTO",
            "requested_date": trading_date.isoformat(),
            "actual_research_date": trading_date.isoformat(),
            "automatic_report_slot": slot,
            "automatic_report_config": frozen_config,
            "research_scope": scope,
            "target_symbols": target_symbols,
            "tracked_symbols": target_symbols,
            "research_budget": budget,
            "portfolio_requested": portfolio_requested,
            "research_only_reason": (
                None
                if portfolio_requested
                else f"定向研究标的少于 {target_count} 只，正式个股研究正常完成但不生成整体组合"
            ),
            "snapshot_mode": "SYSTEM_ENFORCED",
        }
        run.user_id = user_id
        run.status = "PENDING"
        run.idempotency_key = idempotency_key
        run.active_research_key = active_key
        run.manifest = manifest
        run.input_hash = stable_hash(manifest)
        AuditLogger(session).record(
            run_id,
            "AUTO_RESEARCH_SUBMITTED",
            "Automatic daily research queued after market data became ready",
            details={
                "user_id": user_id,
                "requested_date": trading_date.isoformat(),
                "actual_research_date": trading_date.isoformat(),
                "automatic_report_slot": slot,
                "research_scope": scope,
                "target_symbol_count": len(target_symbols),
                "frozen_config": frozen_config,
                "input_hash": run.input_hash,
            },
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            orphan = session.get(JobRun, run_id)
            if orphan is not None:
                orphan.status = "FAILED"
                orphan.active_research_key = None
                orphan.error_message = "deduplicated by automatic research idempotency"
                orphan.completed_at = datetime.now(UTC)
                session.commit()
            return None
    try:
        enqueue(run_id)
    except Exception as exc:
        with session_factory() as session:
            run = session.get(JobRun, run_id)
            if run is not None:
                run.status = "FAILED"
                run.active_research_key = None
                run.error_message = safe_error_message(exc)
                run.completed_at = datetime.now(UTC)
                AuditLogger(session).record(
                    run_id,
                    "AUTO_RESEARCH_ENQUEUE_FAILED",
                    "Automatic research could not be queued",
                    severity="ERROR",
                    details={"error_type": type(exc).__name__},
                )
                session.commit()
        raise
    return run_id


def _automatic_run_key(
    *,
    kind: str,
    user_id: str,
    trading_date: date,
    slot: str,
    trigger_source: str | None = None,
) -> str:
    identity: dict[str, Any] = {
        "kind": kind,
        "user_id": user_id,
        "trading_date": trading_date,
    }
    if trigger_source is not None:
        identity["trigger_source"] = trigger_source
    # Slot A replaces the legacy single automatic report and deliberately
    # retains its key so a mid-window deployment cannot submit it twice.
    if slot != "A":
        identity["automatic_report_slot"] = slot
    return stable_hash(identity)


def _configured_portfolio_target_count() -> int:
    path = get_settings().policy_config_path
    try:
        value = json.loads(path.read_bytes())["portfolio"]["target_count"]
        target_count = int(value)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("versioned portfolio target_count is unavailable") from exc
    if target_count <= 0:
        raise RuntimeError("portfolio target_count must be positive")
    return target_count


def _date_value(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip().replace("/", "-")
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _aware_shanghai(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=SHANGHAI)
        if value.tzinfo is None
        else value.astimezone(SHANGHAI)
    )
