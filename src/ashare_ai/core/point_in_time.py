from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Protocol, TypeVar

from pydantic import AwareDatetime

from ashare_ai.core.contracts import PointInTimeRecord
from ashare_ai.core.time import assert_available

T = TypeVar("T", bound=PointInTimeRecord)


class HasRevision(Protocol):
    revision_seq: int


def visible_as_of(records: Iterable[T], decision_at: AwareDatetime) -> list[T]:
    return [record for record in records if record.available_at <= decision_at]


def assert_records_visible(
    records: Iterable[PointInTimeRecord], decision_at: AwareDatetime
) -> None:
    for record in records:
        assert_available(record.available_at, decision_at)


def latest_visible(
    records: Iterable[T],
    decision_at: AwareDatetime,
    *,
    trading_date: date | None = None,
) -> T | None:
    visible = [
        record
        for record in records
        if record.available_at <= decision_at
        and (trading_date is None or record.trading_date <= trading_date)
    ]
    return max(visible, key=lambda item: (item.trading_date, item.available_at), default=None)


def point_in_time_join(
    left: Sequence[T], right: Sequence[T], decision_at: AwareDatetime
) -> list[tuple[T, T]]:
    """Join records by symbol/date after applying the same explicit cutoff to both sides."""
    left_visible = visible_as_of(left, decision_at)
    right_index = {
        (item.symbol, item.trading_date): item for item in visible_as_of(right, decision_at)
    }
    return [
        (item, right_index[(item.symbol, item.trading_date)])
        for item in left_visible
        if (item.symbol, item.trading_date) in right_index
    ]
