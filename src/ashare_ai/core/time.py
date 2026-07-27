from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime

SHANGHAI = ZoneInfo("Asia/Shanghai")

MarketSessionState = Literal["OPEN", "PRE_OPEN", "BREAK", "CLOSED", "UNKNOWN"]
MarketSessionReason = Literal[
    "TRADING_SESSION",
    "BEFORE_OPEN",
    "MIDDAY_BREAK",
    "AFTER_CLOSE",
    "NON_TRADING_DAY",
    "CALENDAR_UNAVAILABLE",
]


@dataclass(frozen=True)
class MarketSession:
    """Current A-share session state derived from an exchange calendar."""

    state: MarketSessionState
    is_trading_day: bool | None
    reason: MarketSessionReason


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value


def to_utc(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(UTC)


def conservative_date_availability(value: date) -> datetime:
    return datetime.combine(value, time(23, 59, 59, 999999), tzinfo=SHANGHAI)


def market_decision_time(value: date, hour: int = 18, minute: int = 0) -> datetime:
    return datetime.combine(value, time(hour, minute), tzinfo=SHANGHAI)


def market_session(now: datetime, sessions: set[date] | tuple[date, ...] | None) -> MarketSession:
    """Classify the Shanghai A-share session without making external calls.

    ``sessions`` is intentionally supplied by the caller so this core rule stays
    deterministic and can fail closed when the exchange calendar is unavailable.
    """

    current = ensure_aware(now).astimezone(SHANGHAI)
    if sessions is None:
        return MarketSession("UNKNOWN", None, "CALENDAR_UNAVAILABLE")
    if current.date() not in sessions:
        return MarketSession("CLOSED", False, "NON_TRADING_DAY")

    clock = current.time().replace(tzinfo=None)
    if clock < time(9, 30):
        return MarketSession("PRE_OPEN", True, "BEFORE_OPEN")
    if clock < time(11, 30):
        return MarketSession("OPEN", True, "TRADING_SESSION")
    if clock < time(13, 0):
        return MarketSession("BREAK", True, "MIDDAY_BREAK")
    if clock < time(15, 0):
        return MarketSession("OPEN", True, "TRADING_SESSION")
    return MarketSession("CLOSED", True, "AFTER_CLOSE")


def assert_available(available_at: AwareDatetime, decision_at: AwareDatetime) -> None:
    if available_at > decision_at:
        raise ValueError(
            f"future information rejected: available_at={available_at.isoformat()} "
            f"> decision_at={decision_at.isoformat()}"
        )
