from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

import ashare_ai.market.service as market_service
from ashare_ai.core.config import Settings
from ashare_ai.market.service import MarketDataService

_FULL_START = date(1990, 1, 1)
_FULL_END = date(2100, 1, 1)
# A window of a single trading week (Mon 13th .. Fri 17th July 2026).
_WINDOW = (date(2026, 7, 13), date(2026, 7, 17))
_EXPECTED_WEEK = (
    date(2026, 7, 13),
    date(2026, 7, 14),
    date(2026, 7, 15),
    date(2026, 7, 16),
    date(2026, 7, 17),
)


def _weekdays(start: date, end: date) -> tuple[date, ...]:
    """Sparse stand-in for a trading calendar: all weekdays in range."""
    result: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return tuple(result)


class CalendarProvider:
    """Counting provider that can produce any window of weekdays."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_call: tuple[date, date] | None = None

    def sessions(self, start_date: date, end_date: date) -> tuple[date, ...]:
        self.calls += 1
        self.last_call = (start_date, end_date)
        return _weekdays(start_date, end_date)


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class FakeRedis:
    """Dict-backed Redis double with just enough of the surface the calendar
    cache path touches (``get``/``setex``)."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self.values.get(key)

    def setex(self, key: str, seconds: int, value: str) -> None:
        del seconds
        with self._lock:
            self.values[key] = value


def _service(
    provider: CalendarProvider, *, clock: FixedClock, redis: FakeRedis | None
) -> MarketDataService:
    return MarketDataService(
        primary=provider,
        settings=Settings(_env_file=None),
        clock=clock.now,
        redis_client=redis,
    )


def test_sessions_caches_the_full_calendar_and_slices_per_window() -> None:
    # 2026-07-17 05:00 UTC == 13:00 Shanghai, before close, so this is a plain
    # provider read (no isolated after-close path involved).
    clock = FixedClock(datetime(2026, 7, 17, 5, 0, tzinfo=UTC))
    provider = CalendarProvider()
    service = _service(provider, clock=clock, redis=None)
    try:
        first = service.sessions(_WINDOW[0], _WINDOW[1])
        assert provider.calls == 1
        assert first == _EXPECTED_WEEK

        # The provider was asked for the whole calendar, not the window.
        assert provider.last_call == (_FULL_START, _FULL_END)

        # A second, different window reuses the same cached record: still one fetch.
        second = service.sessions(date(2026, 7, 6), date(2026, 7, 10))
        assert provider.calls == 1
        assert second == (
            date(2026, 7, 6),
            date(2026, 7, 7),
            date(2026, 7, 8),
            date(2026, 7, 9),
            date(2026, 7, 10),
        )
    finally:
        service.close()


def test_sessions_refetches_once_the_calendar_cache_expires() -> None:
    clock = FixedClock(datetime(2026, 7, 17, 5, 0, tzinfo=UTC))
    provider = CalendarProvider()
    service = _service(provider, clock=clock, redis=None)
    try:
        service.sessions(_WINDOW[0], _WINDOW[1])
        assert provider.calls == 1

        # Advance just past the 6h cache TTL: the next read must refetch.
        clock.advance(timedelta(seconds=6 * 60 * 60 + 1))
        service.sessions(_WINDOW[0], _WINDOW[1])
        assert provider.calls == 2
    finally:
        service.close()


def test_sessions_shares_one_fetch_across_service_instances_via_redis() -> None:
    clock = FixedClock(datetime(2026, 7, 17, 5, 0, tzinfo=UTC))
    provider = CalendarProvider()
    shared = FakeRedis()
    first = _service(provider, clock=clock, redis=shared)
    second = _service(provider, clock=clock, redis=shared)
    try:
        expected = first.sessions(_WINDOW[0], _WINDOW[1])
        assert provider.calls == 1

        # A fresh instance with an empty local cache reads the record from Redis
        # instead of fetching AKShare again.
        actual = second.sessions(_WINDOW[0], _WINDOW[1])
        assert provider.calls == 1
        assert actual == expected
    finally:
        first.close()
        second.close()


def test_sessions_coalesces_concurrent_fetches_onto_one_provider_call() -> None:
    clock = FixedClock(datetime(2026, 7, 17, 5, 0, tzinfo=UTC))
    provider = CalendarProvider()
    service = _service(provider, clock=clock, redis=None)
    try:
        def call() -> tuple[date, ...]:
            return service.sessions(_WINDOW[0], _WINDOW[1])

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call) for _ in range(2)]
            results = [future.result() for future in futures]

        assert provider.calls == 1
        assert results[0] == results[1] == _EXPECTED_WEEK
    finally:
        service.close()


class IsolatedStub:
    """Stand-in for ``service.AKShareMarketProvider`` swapped in by monkeypatch.

    Records construction and calls so a test can prove the after-close isolated
    path ran while the in-process primary stayed untouched.
    """

    instances = 0
    last_full_call: tuple[date, date] | None = None

    def __init__(self, *, timeout_seconds: float) -> None:
        IsolatedStub.instances += 1
        self.timeout_seconds = timeout_seconds

    def sessions(self, start_date: date, end_date: date) -> tuple[date, ...]:
        IsolatedStub.last_full_call = (start_date, end_date)
        return (date(2026, 7, 14),)

    def close(self) -> None:
        pass


def test_sessions_uses_isolated_provider_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    IsolatedStub.instances = 0
    IsolatedStub.last_full_call = None
    monkeypatch.setattr(market_service, "AKShareMarketProvider", IsolatedStub)

    # Saturday 2026-07-18: is_after_close() is true regardless of the clock time.
    clock = FixedClock(datetime(2026, 7, 18, 10, 0, tzinfo=UTC))
    primary = IsolatedStub(timeout_seconds=1.0)
    service = MarketDataService(
        primary=primary,
        settings=Settings(_env_file=None, api_runtime_auto_close=True),
        clock=clock.now,
        redis_client=None,
    )
    try:
        result = service.sessions(_WINDOW[0], _WINDOW[1])
        # The isolated stub (constructed inside _fetch_calendar) served the data;
        # the primary's own sessions() was never invoked.
        assert result == (date(2026, 7, 14),)
        assert IsolatedStub.last_full_call == (_FULL_START, _FULL_END)
        assert IsolatedStub.instances >= 2
    finally:
        service.close()
