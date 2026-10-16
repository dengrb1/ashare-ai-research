"""Deterministic, request-scoped live position monitoring.

The monitor deliberately keeps its state in the request and in bounded provider
caches.  It does not write research scores or create trades.  This makes the
same implementation suitable for the web UI, mobile clients, and an optional
background process on small machines.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any

from ashare_ai.core.time import SHANGHAI, market_session
from ashare_ai.features.signals import detect_monitor_signals
from ashare_ai.market.service import get_market_data_service, normalize_symbol
from ashare_ai.orchestration.research_schedule import FreeExchangeCalendar
from ashare_ai.portfolio.monitoring import derive_stop_loss_price
from ashare_ai.portfolio.user_assets import UserAssetService
from ashare_ai.storage.database import SessionLocal

MONITOR_VERSION = "realtime-position-v1"
MAX_MONITOR_SYMBOLS = 15
DEFAULT_STALE_AFTER_SECONDS = 30
_SESSION_CACHE: dict[Any, tuple[float, set[Any] | None]] = {}
_SESSION_CACHE_LOCK = Lock()


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _quote_collected_at(quote: dict[str, Any], decision_at: datetime) -> datetime:
    status = quote.get("status") if isinstance(quote.get("status"), dict) else {}
    raw = status.get("collected_at") or quote.get("collected_at")
    if isinstance(raw, datetime):
        parsed = raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        return min(parsed, decision_at)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            return min(aware, decision_at)
        except ValueError:
            pass
    return decision_at


def _quote_status(quote: dict[str, Any], decision_at: datetime) -> dict[str, Any]:
    status = quote.get("status") if isinstance(quote.get("status"), dict) else {}
    collected_at = _quote_collected_at(quote, decision_at)
    age_seconds = max(0.0, (decision_at - collected_at.astimezone(UTC)).total_seconds())
    stale = bool(status.get("stale") or status.get("delayed")) or (
        age_seconds > DEFAULT_STALE_AFTER_SECONDS
    )
    return {
        "source": str(status.get("source") or quote.get("source") or "unknown"),
        "collected_at": _iso(collected_at),
        "cached_at": str(status.get("cached_at") or quote.get("cached_at") or _iso(decision_at)),
        "delayed": bool(status.get("delayed") or quote.get("delayed")),
        "stale": stale,
        "age_seconds": round(age_seconds, 1),
        "message": status.get("message"),
    }


def _risk_level(state: str) -> str:
    if state in {"STOP_LOSS_TRIGGERED", "TRAILING_STOP_TRIGGERED", "DRAWDOWN_ESCALATED"}:
        return "CRITICAL"
    if state in {"STOP_LOSS_APPROACHING", "TAKE_PROFIT_REACHED", "DATA_STALE"}:
        return "WARNING"
    return "NORMAL"


def assess_position(
    position: dict[str, Any],
    quote: dict[str, Any] | None,
    *,
    decision_at: datetime,
    atr_bars: list[dict[str, Any]] | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Return one deterministic position assessment suitable for JSON transport."""

    if decision_at.tzinfo is None:
        raise ValueError("decision_at must include a timezone")
    symbol = normalize_symbol(str(position.get("symbol") or ""))
    cost = _decimal(position.get("cost"))
    quantity = max(0, int(_decimal(position.get("quantity"))))
    quote = quote if isinstance(quote, dict) else {}
    price = _decimal(quote.get("price"))
    quote_status = _quote_status(quote, decision_at)
    quote_status["stale"] = quote_status["stale"] or (
        quote_status["age_seconds"] > stale_after_seconds
    )
    if cost <= 0 or price <= 0 or quantity <= 0:
        return {
            "symbol": symbol,
            "name": str(position.get("name") or symbol),
            "quantity": quantity,
            "cost": float(cost),
            "price": float(price) if price else None,
            "market_value": None,
            "unrealized_pnl": None,
            "unrealized_pnl_pct": None,
            "stop_loss_price": None,
            "stop_distance_pct": None,
            "risk_state": "DATA_STALE",
            "risk_level": "WARNING",
            "strategy_version": MONITOR_VERSION,
            "quote_status": quote_status,
            "available_at": quote_status["collected_at"],
            "decision_at": _iso(decision_at),
            "evidence": {"reason": "position_or_quote_incomplete"},
        }

    threshold = derive_stop_loss_price(position, atr_bars or [])
    market_value = price * quantity
    pnl = (price - cost) * quantity
    pnl_pct = price / cost - Decimal("1")
    stop_price = threshold.price
    # Provider-native intraday high gives the trailing policy a useful high-water
    # mark without adding a write-heavy per-tick position column.
    high_water = max(
        price,
        _decimal(position.get("high_water_price"), price),
        _decimal(quote.get("high"), price),
    )
    trailing_price = (
        (high_water * Decimal("0.93")).quantize(Decimal("0.01"))
        if high_water >= cost * Decimal("1.05")
        else Decimal("0")
    )
    profit_trigger_amount = _decimal(position.get("profit_trigger_amount"))
    exit_trigger_price = _decimal(position.get("exit_trigger_price"))
    if quote_status["stale"]:
        state = "DATA_STALE"
    elif bool(position.get("stop_loss_enabled", True)) and price <= stop_price:
        state = "STOP_LOSS_TRIGGERED"
    elif trailing_price > 0 and price <= trailing_price:
        state = "TRAILING_STOP_TRIGGERED"
    elif bool(position.get("stop_loss_enabled", True)) and price <= stop_price * Decimal("1.02"):
        state = "STOP_LOSS_APPROACHING"
    elif (profit_trigger_amount > 0 and pnl >= profit_trigger_amount) or (
        exit_trigger_price > 0 and price >= exit_trigger_price
    ):
        state = "TAKE_PROFIT_REACHED"
    elif pnl_pct <= Decimal("-0.05"):
        state = "DRAWDOWN_ESCALATED"
    else:
        state = "NORMAL"

    distance_pct = (
        (price / stop_price - Decimal("1")) * Decimal("100")
        if stop_price
        else Decimal("0")
    )
    return {
        "symbol": symbol,
        "name": str(position.get("name") or symbol),
        "quantity": quantity,
        "cost": float(cost),
        "price": float(price),
        "change": quote.get("change"),
        "change_percent": quote.get("change_percent"),
        "market_value": float(market_value),
        "unrealized_pnl": float(pnl),
        "unrealized_pnl_pct": round(float(pnl_pct * Decimal("100")), 4),
        "stop_loss_price": float(stop_price),
        "stop_loss_mode": threshold.mode,
        "trailing_stop_price": float(trailing_price) if trailing_price else None,
        "stop_distance_pct": round(float(distance_pct), 4),
        "risk_state": state,
        "risk_level": _risk_level(state),
        "strategy_version": MONITOR_VERSION,
        "quote_status": quote_status,
        "available_at": quote_status["collected_at"],
        "decision_at": _iso(decision_at),
        "evidence": {
            "stop_loss_price": float(stop_price),
            "high_water_price": float(high_water),
            "trailing_stop_price": float(trailing_price) if trailing_price else None,
            "stale_after_seconds": stale_after_seconds,
        },
    }


def _calendar_sessions(now: datetime) -> set[Any] | None:
    day = now.astimezone(SHANGHAI).date()
    current = time.monotonic()
    with _SESSION_CACHE_LOCK:
        cached = _SESSION_CACHE.get(day)
        if cached is not None and current - cached[0] < 3600:
            return cached[1]
    try:
        result: set[Any] | None = set(FreeExchangeCalendar().sessions(day, day))
    except Exception:
        result = None
    with _SESSION_CACHE_LOCK:
        _SESSION_CACHE[day] = (current, result)
        if len(_SESSION_CACHE) > 4:
            oldest = min(_SESSION_CACHE, key=lambda item: _SESSION_CACHE[item][0])
            _SESSION_CACHE.pop(oldest, None)
    return result


def build_live_snapshot(
    user_id: str,
    *,
    now: datetime | None = None,
    refresh: bool = False,
    include_watchlist: bool = False,
    market: Any | None = None,
    session_factory: Any = SessionLocal,
) -> dict[str, Any]:
    """Build a bounded live snapshot for REST, SSE, and native mobile clients."""

    decision_at = (now or datetime.now(UTC)).astimezone(UTC)
    with session_factory() as session:
        assets = UserAssetService(session).get(user_id)
    positions = [dict(item) for item in assets.get("positions", [])][:MAX_MONITOR_SYMBOLS]
    # Keep the legacy flat watchlist in the asset contract, while using only
    # enabled groups as live monitoring inputs.
    watchlist = [str(item) for item in assets.get("watchlist", [])]
    enabled_groups = [
        group for group in (assets.get("watchlist_groups") or [])
        if group.get("enabled", True)
    ]
    monitored_watchlist = list(dict.fromkeys(
        str(symbol)
        for group in enabled_groups
        for symbol in group.get("symbols", [])
    ))
    if not enabled_groups:
        monitored_watchlist = watchlist
    policy = dict(assets.get("monitoring_policy") or {})
    symbol_policies: dict[str, dict[str, Any]] = {}
    for group in enabled_groups:
        override = dict(group.get("monitoring_policy") or {})
        for symbol in group.get("symbols", []):
            symbol_policies[str(symbol)] = {**policy, **override}
    symbols = list(dict.fromkeys(
        [str(item.get("symbol")) for item in positions if item.get("symbol")]
        + (monitored_watchlist if include_watchlist else [])
    ))[:MAX_MONITOR_SYMBOLS]
    service = market or get_market_data_service()
    try:
        quotes = {
            str(item.get("symbol")): item
            for item in service.quotes(symbols, force_refresh=refresh)
            if isinstance(item, dict) and item.get("symbol")
        }
        market_error = None
    except Exception:
        quotes = {}
        market_error = "market data unavailable"
    assessments = [
        assess_position(
            item,
            quotes.get(str(item.get("symbol"))),
            decision_at=decision_at,
        )
        for item in positions
    ]
    assessment_symbols = {item["symbol"] for item in assessments}
    watch_quotes = [
        quotes[symbol]
        for symbol in monitored_watchlist
        if symbol in quotes and symbol not in assessment_symbols
    ]
    sessions = _calendar_sessions(decision_at.astimezone(SHANGHAI))
    session = market_session(decision_at, sessions)
    events = [
        {
            "symbol": item["symbol"],
            "signal_type": item["risk_state"],
            "severity": item["risk_level"],
            "evidence": item["evidence"],
            "available_at": item["available_at"],
            "decision_at": item["decision_at"],
        }
        for item in assessments
        if item["risk_state"] != "NORMAL"
    ]
    anomaly_signals: list[dict[str, Any]] = []
    if policy.get("enabled", True) and session.state == "OPEN":
        for symbol in symbols:
            symbol_policy = symbol_policies.get(symbol, policy)
            if not symbol_policy.get("enabled", True):
                continue
            thresholds = {
                "intraday_drop": -abs(float(symbol_policy.get("price_change_pct", 2.0))) / 100.0,
                "breakout_volume_ratio": float(symbol_policy.get("volume_multiplier", 3.0)),
                "divergence_price_pct": float(symbol_policy.get("divergence_price_pct", 1.0)) / 100.0,
                "divergence_volume_ratio": -abs(float(symbol_policy.get("divergence_volume_ratio", 0.5))),
            }
            try:
                payload = service.klines(symbol, "1m", limit=60, force_refresh=refresh)
                status = payload.get("status", {}) if isinstance(payload, dict) else {}
                collected_at = status.get("collected_at") if isinstance(status, dict) else None
                if isinstance(collected_at, str):
                    collected_at = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
                collected_at = collected_at if isinstance(collected_at, datetime) and collected_at.tzinfo else decision_at
                rows = detect_monitor_signals(symbol=symbol, bars=list(payload.get("bars", [])), available_at=collected_at, decision_at=decision_at, thresholds=thresholds)
                for signal in rows:
                    if signal.signal_type == "INTRADAY_DROP" and not symbol_policy.get("price_enabled", True):
                        continue
                    if signal.signal_type == "VOLUME_BREAKOUT" and not symbol_policy.get("volume_enabled", True):
                        continue
                    if signal.signal_type == "VOLUME_PRICE_DIVERGENCE" and not symbol_policy.get("divergence_enabled", True):
                        continue
                    payload_signal = signal.model_dump(mode="json")
                    change = abs(float(signal.evidence.get("primary", 0))) * 100
                    payload_signal["severity"] = "HIGH" if change >= float(symbol_policy.get("high_severity_price_change_pct", 5.0)) else signal.severity
                    anomaly_signals.append(payload_signal)
            except (ValueError, RuntimeError, KeyError, TypeError):
                continue
    events.extend(anomaly_signals)
    return {
        "monitor_version": MONITOR_VERSION,
        "generated_at": _iso(decision_at),
        "decision_at": _iso(decision_at),
        "session": {
            "state": session.state,
            "is_trading_day": session.is_trading_day,
            "reason": session.reason,
        },
        # The desktop anomaly stream is intentionally minute based.  The
        # legacy asset refresh preference still controls ordinary quote
        # prefetching, but must not advertise a faster anomaly cadence.
        "next_poll_seconds": 60,
        "positions": assessments,
        "watchlist_quotes": watch_quotes,
        "signals": events,
        "monitoring_policy": policy,
        "market_error": market_error,
        "stale": any(item["risk_state"] == "DATA_STALE" for item in assessments),
        "research_snapshot_isolated": True,
    }
