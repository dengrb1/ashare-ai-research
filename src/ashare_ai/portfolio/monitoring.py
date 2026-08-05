from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.market.service import get_market_data_service
from ashare_ai.notifications.service import NotificationService
from ashare_ai.orchestration.exit_advice_jobs import (
    create_stop_loss_exit_advice,
    enqueue_exit_advice,
)
from ashare_ai.orchestration.research_schedule import FreeExchangeCalendar
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    BuyEntryMonitorRow,
    ExitAdviceRow,
    JobRun,
    ScoreRow,
    TradePlanRow,
    UserAccount,
    UserAssetState,
)


@dataclass(frozen=True)
class StopLossThreshold:
    price: Decimal
    mode: str
    loss_pct: Decimal
    atr: Decimal | None


def derive_stop_loss_price(
    position: dict[str, Any], daily_bars: list[dict[str, Any]] | None = None,
    *,
    atr_sessions: int = 20,
    atr_multiple: Decimal = Decimal("2"),
    min_loss_pct: Decimal = Decimal("0.05"),
    max_loss_pct: Decimal = Decimal("0.10"),
    fallback_loss_pct: Decimal = Decimal("0.08"),
) -> StopLossThreshold:
    """Derive a cost-bounded paper stop from post-adjusted daily ATR data."""

    cost = Decimal(str(position.get("cost") or 0))
    if cost <= 0:
        raise ValueError("position cost must be positive")
    manual = position.get("stop_loss_price")
    if manual not in (None, ""):
        value = Decimal(str(manual))
        if value > 0:
            # A manual value is constrained to the same 5%--10% loss band as ATR.
            # This stops an accidental far-below-cost entry from silently weakening
            # the paper-risk monitor.
            bounded = _clamp(
                value,
                cost * (Decimal("1") - max_loss_pct),
                cost * (Decimal("1") - min_loss_pct),
            )
            return StopLossThreshold(
                price=_money(bounded),
                mode="MANUAL",
                loss_pct=_clamp(Decimal("1") - bounded / cost, min_loss_pct, max_loss_pct),
                atr=None,
            )
    bars = list(daily_bars or [])[-max(atr_sessions + 1, 2) :]
    atr = _atr(bars, atr_sessions)
    if atr is not None and atr > 0:
        loss_pct = _clamp(atr * atr_multiple / cost, min_loss_pct, max_loss_pct)
        return StopLossThreshold(
            price=_money(cost * (Decimal("1") - loss_pct)),
            mode="AUTO_ATR20",
            loss_pct=loss_pct,
            atr=atr,
        )
    fallback = _clamp(fallback_loss_pct, min_loss_pct, max_loss_pct)
    return StopLossThreshold(
        price=_money(cost * (Decimal("1") - fallback)),
        mode="FALLBACK_8PCT",
        loss_pct=fallback,
        atr=None,
    )


class StopLossMonitorService:
    """Create high-priority paper-risk notifications before exit research is queued."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        market: Any | None = None,
        calendar: Any | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.settings = settings or get_settings()
        self.market = market or get_market_data_service()
        self.calendar = calendar or FreeExchangeCalendar()
        self.session_factory = session_factory

    def dispatch(
        self,
        *,
        now: datetime,
        enqueue: Callable[[str], None] = enqueue_exit_advice,
    ) -> dict[str, Any]:
        current = now.astimezone(SHANGHAI)
        if not _during_session(current):
            return {"state": "OUTSIDE_SESSION", "queued": [], "notified": []}
        calendar_state = _trading_day_state(self.calendar, current)
        if calendar_state is None:
            return {"state": "CALENDAR_UNAVAILABLE", "queued": [], "notified": []}
        if not calendar_state:
            return {"state": "NON_TRADING_DAY", "queued": [], "notified": []}
        with self.session_factory() as session:
            states = list(
                session.scalars(
                    select(UserAssetState)
                    .join(UserAccount, UserAccount.user_id == UserAssetState.user_id)
                    .where(
                        UserAssetState.stop_loss_monitor_enabled.is_(True),
                        UserAccount.enabled.is_(True),
                    )
                ).all()
            )
        pairs = [
            (state.user_id, dict(position))
            for state in states
            for position in state.positions
            if bool(position.get("stop_loss_enabled", True))
            and str(position.get("symbol") or "")
        ]
        symbols = sorted({str(position["symbol"]) for _, position in pairs})
        if not symbols:
            return {"state": "READY", "queued": [], "notified": []}
        try:
            quotes = {
                str(item.get("symbol")): item
                for item in self.market.quotes(symbols)
                if isinstance(item, dict)
            }
        except Exception:
            quotes = {}
        queued: list[str] = []
        notified: list[str] = []
        for user_id, position in pairs:
            symbol = str(position["symbol"])
            quote = quotes.get(symbol)
            with self.session_factory() as session:
                notifications = NotificationService(session, self.settings)
                if quote is None or not _quote_is_fresh(quote, current, self.settings):
                    row = notifications.create(
                        user_id=user_id,
                        notification_type="STOP_LOSS_DATA_UNAVAILABLE",
                        severity="WARNING",
                        title=f"{symbol} 止损监控缺少新鲜行情",
                        body="本次监控未生成退出建议，请等待下一次行情更新。",
                        resource_type="POSITION",
                        resource_id=symbol,
                        dedupe_key=f"stop-data:{user_id}:{symbol}:{current.date().isoformat()}",
                    )
                    session.commit()
                    notified.append(row.notification_id)
                    continue
                price = Decimal(str(quote.get("price")))
                cost = Decimal(str(position.get("cost") or 0))
                if cost <= 0:
                    continue
                # A non-manual stop is at least 5% below cost, so avoid an ATR fetch
                # for the overwhelmingly common non-triggering quote.
                if position.get("stop_loss_price") in (None, "") and price > cost * Decimal("0.95"):
                    continue
                bars: list[dict[str, Any]] = []
                if position.get("stop_loss_price") in (None, ""):
                    try:
                        payload = self.market.klines(
                            symbol,
                            "day",
                            limit=21,
                            end=current,
                            adjustment="hfq",
                        )
                        raw_bars = payload.get("bars", []) if isinstance(payload, dict) else []
                        bars = raw_bars if isinstance(raw_bars, list) else []
                    except Exception:
                        bars = []
                policy = _monitoring_policy(self.settings)
                threshold = derive_stop_loss_price(
                    position,
                    bars,
                    atr_sessions=policy["atr_sessions"],
                    atr_multiple=policy["atr_multiple"],
                    min_loss_pct=policy["min_loss_pct"],
                    max_loss_pct=policy["max_loss_pct"],
                    fallback_loss_pct=policy["fallback_loss_pct"],
                )
                if price > threshold.price:
                    continue
                latest = session.scalar(
                    select(ExitAdviceRow)
                    .where(
                        ExitAdviceRow.user_id == user_id,
                        ExitAdviceRow.symbol == symbol,
                        ExitAdviceRow.trigger_type == "STOP_LOSS",
                        ExitAdviceRow.created_at
                        >= current.astimezone(UTC)
                        - timedelta(minutes=policy["cooldown_minutes"]),
                    )
                    .order_by(ExitAdviceRow.created_at.desc())
                    .limit(1)
                )
                if latest is not None:
                    continue
                trigger_slot = int(
                    current.astimezone(UTC).timestamp()
                    // (policy["cooldown_minutes"] * 60)
                )
                notification = notifications.create(
                    user_id=user_id,
                    notification_type="STOP_LOSS_TRIGGERED",
                    severity="CRITICAL",
                    title=f"{symbol} 已触发模拟止损预警",
                    body=(
                        f"最新价 {price} 已到达止损线 {threshold.price}。"
                        "系统将生成模拟退出研究，不会自动卖出或修改持仓。"
                    ),
                    resource_type="POSITION",
                    resource_id=symbol,
                    payload={
                        "symbol": symbol,
                        "price": str(price),
                        "stop_loss_price": str(threshold.price),
                        "mode": threshold.mode,
                    },
                    dedupe_key=f"stop-trigger:{user_id}:{symbol}:{trigger_slot}",
                )
                # The alert is committed before enqueueing the AI work by design.
                session.commit()
                notified.append(notification.notification_id)
            try:
                advice = create_stop_loss_exit_advice(
                    user_id=user_id,
                    symbol=symbol,
                    position=position,
                    quote=quote,
                    stop_loss_price=threshold.price,
                    now=current,
                    enqueue=enqueue,
                    session_factory=self.session_factory,
                )
                queued.append(advice.advice_id)
            except Exception:
                with self.session_factory() as session:
                    NotificationService(session, self.settings).create(
                        user_id=user_id,
                        notification_type="STOP_LOSS_RESEARCH_UNAVAILABLE",
                        severity="WARNING",
                        title=f"{symbol} 止损退出研究未能提交",
                        body="行情预警已记录；交易规则或研究上下文暂不可用。",
                        resource_type="POSITION",
                        resource_id=symbol,
                        dedupe_key=f"stop-research:{user_id}:{symbol}:{current.date().isoformat()}",
                    )
                    session.commit()
        return {"state": "READY", "queued": queued, "notified": notified}


class BuyEntryMonitorService:
    """Persist and watch next-session entry bands from formal BUY trade plans."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        market: Any | None = None,
        calendar: Any | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.settings = settings or get_settings()
        self.market = market or get_market_data_service()
        self.calendar = calendar or FreeExchangeCalendar()
        self.session_factory = session_factory

    def refresh_from_trade_plans(self, *, now: datetime) -> int:
        """Materialize only formal, user-owned BUY plans for watched securities."""
        created = 0
        current = now.astimezone(SHANGHAI)
        with self.session_factory() as session:
            states = list(
                session.scalars(
                    select(UserAssetState)
                    .join(UserAccount, UserAccount.user_id == UserAssetState.user_id)
                    .where(
                        UserAssetState.buy_monitor_enabled.is_(True),
                        UserAccount.enabled.is_(True),
                    )
                ).all()
            )
            for state in states:
                watchlist = set(state.watchlist)
                if not watchlist:
                    continue
                plans = list(
                    session.scalars(
                        select(TradePlanRow)
                        .where(
                            TradePlanRow.user_id == state.user_id,
                            TradePlanRow.status == "SUCCEEDED",
                        )
                        .order_by(TradePlanRow.completed_at.desc())
                        .limit(20)
                    ).all()
                )
                for plan in plans:
                    result = plan.deterministic_result or {}
                    for item in result.get("symbol_plans", []) if isinstance(result, dict) else []:
                        if not isinstance(item, dict):
                            continue
                        symbol = str(item.get("symbol") or "")
                        if symbol not in watchlist or str(item.get("outcome")) != "BUY":
                            continue
                        if not _has_formal_score(session, state.user_id, symbol, plan.decision_at):
                            continue
                        try:
                            effective_date = date.fromisoformat(str(item["entry_valid_from"]))
                            valid_until = date.fromisoformat(str(item["entry_valid_until"]))
                            low = Decimal(str(item["limit_price_low"]))
                            high = Decimal(str(item["limit_price_high"]))
                        except (KeyError, ValueError, ArithmeticError):
                            continue
                        if high < low or valid_until < effective_date:
                            continue
                        monitor_dates = _buy_monitor_dates(
                            self.calendar,
                            plan.decision_at.astimezone(SHANGHAI).date(),
                            effective_date,
                            valid_until,
                            _monitoring_policy(self.settings)["buy_monitor_valid_sessions"],
                        )
                        if monitor_dates is None:
                            continue
                        expires_at = datetime.combine(
                            monitor_dates[-1], time(15, 5), tzinfo=SHANGHAI
                        ).astimezone(UTC)
                        if expires_at <= current.astimezone(UTC):
                            continue
                        existing = session.scalar(
                            select(BuyEntryMonitorRow).where(
                                BuyEntryMonitorRow.user_id == state.user_id,
                                BuyEntryMonitorRow.symbol == symbol,
                                BuyEntryMonitorRow.effective_date == effective_date,
                            )
                        )
                        if existing is not None:
                            continue
                        session.add(
                            BuyEntryMonitorRow(
                                user_id=state.user_id,
                                symbol=symbol,
                                status="ACTIVE",
                                effective_date=effective_date,
                                expires_at=expires_at,
                                entry_low=low,
                                entry_high=high,
                                score_run_id=plan.run_id,
                                trade_plan_id=plan.plan_id,
                                rationale={
                                    "outcome": "BUY",
                                    "source_plan_id": plan.plan_id,
                                    "reference_price": item.get("reference_price"),
                                    "decision_at": plan.decision_at.isoformat(),
                                },
                                created_at=current.astimezone(UTC),
                                updated_at=current.astimezone(UTC),
                            )
                        )
                        created += 1
            if created:
                session.commit()
        return created

    def dispatch(self, *, now: datetime) -> dict[str, Any]:
        current = now.astimezone(SHANGHAI)
        if not _during_session(current):
            return {"state": "OUTSIDE_SESSION", "notified": []}
        calendar_state = _trading_day_state(self.calendar, current)
        if calendar_state is None:
            return {"state": "CALENDAR_UNAVAILABLE", "notified": []}
        if not calendar_state:
            return {"state": "NON_TRADING_DAY", "notified": []}
        with self.session_factory() as session:
            monitors = list(
                session.scalars(
                    select(BuyEntryMonitorRow).where(
                        BuyEntryMonitorRow.status == "ACTIVE",
                        BuyEntryMonitorRow.effective_date <= current.date(),
                        BuyEntryMonitorRow.expires_at > current.astimezone(UTC),
                    )
                ).all()
            )
        symbols = sorted({row.symbol for row in monitors})
        try:
            quotes = {
                str(item.get("symbol")): item
                for item in self.market.quotes(symbols)
                if isinstance(item, dict)
            }
        except Exception:
            quotes = {}
        notified: list[str] = []
        with self.session_factory() as session:
            notifications = NotificationService(session, self.settings)
            for monitor in monitors:
                quote = quotes.get(monitor.symbol)
                if quote is None or not _quote_is_fresh(quote, current, self.settings):
                    notification = notifications.create(
                        user_id=monitor.user_id,
                        notification_type="BUY_MONITOR_DATA_UNAVAILABLE",
                        severity="WARNING",
                        title=f"{monitor.symbol} 买入监控缺少新鲜行情",
                        body="本次未判断入场区间，等待下一次行情更新。",
                        resource_type="BUY_ENTRY_MONITOR",
                        resource_id=monitor.monitor_id,
                        dedupe_key=f"buy-data:{monitor.monitor_id}:{current.date().isoformat()}",
                    )
                    monitor.error_code = "QUOTE_UNAVAILABLE"
                    monitor.updated_at = current.astimezone(UTC)
                    notified.append(notification.notification_id)
                    continue
                price = Decimal(str(quote["price"]))
                if monitor.entry_low <= price <= monitor.entry_high:
                    notification = notifications.create(
                        user_id=monitor.user_id,
                        notification_type="BUY_ENTRY_RANGE_HIT",
                        severity="HIGH",
                        title=f"{monitor.symbol} 进入模拟买入区间",
                        body=(
                            f"最新价 {price} 位于正式 Trade Plan 的入场区间 "
                            f"{monitor.entry_low} - {monitor.entry_high}。"
                            "仅生成模拟方案，不自动交易。"
                        ),
                        resource_type="BUY_ENTRY_MONITOR",
                        resource_id=monitor.monitor_id,
                        payload={"symbol": monitor.symbol, "trade_plan_id": monitor.trade_plan_id},
                        dedupe_key=f"buy-hit:{monitor.monitor_id}",
                    )
                    monitor.status = "NOTIFIED"
                    monitor.triggered_at = current.astimezone(UTC)
                    monitor.updated_at = current.astimezone(UTC)
                    notified.append(notification.notification_id)
            session.commit()
        return {"state": "READY", "notified": notified}


def _has_formal_score(session: Session, user_id: str, symbol: str, decision_at: datetime) -> bool:
    return (
        session.scalar(
            select(ScoreRow.score_id)
            .join(JobRun, JobRun.run_id == ScoreRow.run_id)
            .where(
                JobRun.user_id == user_id,
                JobRun.status.in_(("SUCCEEDED", "FUSED")),
                ScoreRow.symbol == symbol,
                ScoreRow.decision_at <= decision_at,
                JobRun.completed_at.is_not(None),
                JobRun.completed_at <= decision_at,
            )
            .limit(1)
        )
        is not None
    )


def _atr(bars: list[dict[str, Any]], sessions: int) -> Decimal | None:
    if len(bars) < 2:
        return None
    ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for bar in bars:
        try:
            high = Decimal(str(bar["high"]))
            low = Decimal(str(bar["low"]))
            close = Decimal(str(bar["close"]))
        except (KeyError, ArithmeticError):
            continue
        if min(high, low, close) <= 0:
            continue
        values = [high - low]
        if previous_close is not None:
            values.extend((abs(high - previous_close), abs(low - previous_close)))
        ranges.append(max(values))
        previous_close = close
    if len(ranges) < sessions:
        return None
    return sum(ranges[-sessions:], Decimal("0")) / Decimal(sessions)


def _monitoring_policy(settings: Settings) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "atr_sessions": 20,
        "atr_multiple": Decimal("2"),
        "min_loss_pct": Decimal("0.05"),
        "max_loss_pct": Decimal("0.10"),
        "fallback_loss_pct": Decimal("0.08"),
        "cooldown_minutes": 30,
        "buy_monitor_valid_sessions": 1,
    }
    try:
        payload = json.loads(Path(settings.policy_config_path).read_text(encoding="utf-8"))
        source = payload.get("monitoring", {}) if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError):
        source = {}
    return {
        "atr_sessions": _int(
            source.get("stop_loss_atr_sessions"), defaults["atr_sessions"], 5, 120
        ),
        "atr_multiple": _decimal(source.get("stop_loss_atr_multiple"), defaults["atr_multiple"]),
        "min_loss_pct": _decimal(source.get("stop_loss_min_pct"), defaults["min_loss_pct"]),
        "max_loss_pct": _decimal(source.get("stop_loss_max_pct"), defaults["max_loss_pct"]),
        "fallback_loss_pct": _decimal(
            source.get("stop_loss_fallback_pct"), defaults["fallback_loss_pct"]
        ),
        "cooldown_minutes": _int(
            source.get("stop_loss_cooldown_minutes"), defaults["cooldown_minutes"], 1, 1440
        ),
        "buy_monitor_valid_sessions": _int(
            source.get("buy_monitor_valid_sessions"),
            defaults["buy_monitor_valid_sessions"],
            1,
            5,
        ),
    }


def _buy_monitor_dates(
    calendar: Any,
    decision_date: date,
    effective_date: date,
    valid_until: date,
    valid_sessions: int,
) -> tuple[date, ...] | None:
    """Return the next-session window without extending a formal Trade Plan."""

    try:
        sessions = sorted(
            calendar.sessions(decision_date + timedelta(days=1), valid_until)
        )
    except Exception:
        return None
    if not sessions or sessions[0] != effective_date:
        return None
    plan_window = tuple(item for item in sessions if effective_date <= item <= valid_until)
    if not plan_window or plan_window[0] != effective_date:
        return None
    return plan_window[:valid_sessions]


def _quote_is_fresh(quote: dict[str, Any], now: datetime, settings: Settings) -> bool:
    raw_status = quote.get("status")
    status: dict[str, Any] = dict(raw_status) if isinstance(raw_status, dict) else {}
    if status.get("stale") or status.get("delayed") or quote.get("price") in (None, ""):
        return False
    raw = status.get("collected_at") or quote.get("collected_at")
    if not raw:
        return True
    try:
        collected = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=SHANGHAI)
    age_seconds = (now.astimezone(UTC) - collected.astimezone(UTC)).total_seconds()
    return age_seconds <= settings.market_stale_seconds


def _during_session(current: datetime) -> bool:
    clock = current.astimezone(SHANGHAI).time().replace(tzinfo=None)
    return time(9, 30) <= clock <= time(11, 30) or time(13, 0) <= clock <= time(15, 0)


def _trading_day_state(calendar: Any, current: datetime) -> bool | None:
    try:
        sessions = calendar.sessions(current.date(), current.date())
    except Exception:
        return None
    return current.date() in sessions


def _decimal(value: Any, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, parsed))


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return min(upper, max(lower, value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
