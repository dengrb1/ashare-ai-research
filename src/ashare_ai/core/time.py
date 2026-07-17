from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime

SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def assert_available(available_at: AwareDatetime, decision_at: AwareDatetime) -> None:
    if available_at > decision_at:
        raise ValueError(
            f"future information rejected: available_at={available_at.isoformat()} "
            f"> decision_at={decision_at.isoformat()}"
        )
