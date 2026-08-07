from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.config import Settings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.market import warmup
from ashare_ai.market.service import MarketDataService
from ashare_ai.storage.models import Base, UserAssetState


def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _swr_settings(**overrides: Any) -> Settings:
    base = dict(
        market_cache_seconds=15,
        market_kline_cache_seconds=15,
        market_stale_seconds=60,
        market_timeout_seconds=1,
    )
    base.update(overrides)
    return Settings(**base)


# --- stale-while-revalidate behaviour ---------------------------------------


class _QuoteProvider:
    source = "akshare"

    def __init__(self, first_price: float = 10.0, later_price: float = 20.0) -> None:
        self.calls = 0
        self.first_price = first_price
        self.later_price = later_price

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        del symbols
        self.calls += 1
        price = self.first_price if self.calls == 1 else self.later_price
        return [{"symbol": "600000.SH", "price": price}]

    def klines(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        return []


class _EmptyFallback:
    source = "empty"

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        del symbols
        return []

    def klines(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        return []


class _KlineProvider:
    source = "sina"

    def __init__(self) -> None:
        self.calls = 0

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        del symbols
        return []

    def klines(
        self, symbol: str, period: str, start: Any, end: Any, limit: int
    ) -> list[dict[str, Any]]:
        del symbol, period, start, end, limit
        self.calls += 1
        return [
            {
                "timestamp": "2026-07-14T00:00:00+08:00",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1000,
            }
        ]


def test_quotes_serve_stale_instantly_and_refresh_in_background() -> None:
    provider = _QuoteProvider()
    current = [datetime(2026, 7, 15, 2, tzinfo=UTC)]
    service = MarketDataService(
        primary=provider,
        fallback=_EmptyFallback(),
        settings=_swr_settings(),
        clock=lambda: current[0],
        redis_client=None,
    )
    # Cold load fills the cache from the provider.
    assert service.quotes(["600000.SH"])[0]["price"] == 10.0
    assert provider.calls == 1
    # Cache expired but is still usable: serve it instantly (stale 10, not a
    # fresh 20) and refresh in the background.
    current[0] += timedelta(seconds=16)
    swr = service.quotes(["600000.SH"])
    assert swr[0]["price"] == 10.0
    assert swr[0]["status"]["cache_hit"] is True
    # The background refresh eventually updates the cache.
    _wait_until(lambda: service.quotes(["600000.SH"])[0]["price"] == 20.0)
    assert provider.calls >= 2


def test_quotes_force_refresh_waits_synchronously() -> None:
    provider = _QuoteProvider()
    current = [datetime(2026, 7, 15, 2, tzinfo=UTC)]
    service = MarketDataService(
        primary=provider,
        fallback=_EmptyFallback(),
        settings=_swr_settings(),
        clock=lambda: current[0],
        redis_client=None,
    )
    assert service.quotes(["600000.SH"])[0]["price"] == 10.0
    current[0] += timedelta(seconds=16)
    # force_refresh bypasses stale-while-revalidate and waits for fresh data.
    fresh = service.quotes(["600000.SH"], force_refresh=True)
    assert fresh[0]["price"] == 20.0
    assert fresh[0]["status"]["cache_hit"] is False


def test_klines_serve_stale_instantly_and_refresh_in_background() -> None:
    provider = _KlineProvider()
    current = [datetime(2026, 7, 15, 2, tzinfo=UTC)]
    service = MarketDataService(
        primary=provider,
        fallback=_EmptyFallback(),
        settings=_swr_settings(),
        clock=lambda: current[0],
        redis_client=None,
    )
    service.klines("600000.SH", "day", limit=5)
    assert provider.calls == 1
    current[0] += timedelta(seconds=16)
    # Stale K-line is served from cache immediately.
    swr = service.klines("600000.SH", "day", limit=5)
    assert swr["status"]["cache_hit"] is True
    _wait_until(lambda: provider.calls >= 2)
    # The background refresh kept the cache usable.
    fresh = service.klines("600000.SH", "day", limit=5)
    assert fresh["status"]["cache_hit"] is True
    assert fresh["bars"][0]["close"] == 10.5


# --- warmup symbol collection ------------------------------------------------


def _asset_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{(tmp_path / 'warmup.db').as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_collect_warmup_symbols_unions_dedups_and_sorts(tmp_path: Path) -> None:
    factory = _asset_factory(tmp_path)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            UserAssetState(
                user_id="u1",
                watchlist=["600519.SH", "000858.SZ"],
                positions=[{"symbol": "300750.SZ"}],
                updated_at=now,
            )
        )
        session.add(
            UserAssetState(
                user_id="u2",
                watchlist=["600519.SH", "601318.SH"],
                positions=[],
                updated_at=now,
            )
        )
        session.commit()
    with factory() as session:
        symbols = warmup.collect_warmup_symbols(session, max_symbols=10)
    assert symbols == ["000858.SZ", "300750.SZ", "600519.SH", "601318.SH"]


def test_collect_warmup_symbols_keeps_extra_prefix_and_bounds(tmp_path: Path) -> None:
    factory = _asset_factory(tmp_path)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            UserAssetState(
                user_id="u1",
                watchlist=["600519.SH", "000858.SZ", "300750.SZ", "601318.SH"],
                positions=[],
                updated_at=now,
            )
        )
        session.commit()
    with factory() as session:
        symbols = warmup.collect_warmup_symbols(
            session,
            max_symbols=3,
            extra_symbols=["000001.SZ", "600519.SH", "000001.SZ"],
        )
    # Extra hot list keeps its order first; the rest is bounded by the cap.
    assert symbols == ["000001.SZ", "600519.SH", "000858.SZ"]


# --- warmup gating ------------------------------------------------------------


def _warm_settings(**overrides: Any) -> Settings:
    base = dict(
        market_warmup_enabled=True,
        market_warmup_interval_minutes=5,
        market_warmup_max_symbols=50,
        market_warmup_index_symbols="",
        market_warmup_kline_limit=160,
        market_warmup_debounce_seconds=300,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _reset_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    warmup._last_warm_at = None
    calls: dict[str, int] = {"count": 0}

    def _record() -> None:
        calls["count"] += 1

    monkeypatch.setattr(warmup, "_warm_background", _record)
    yield calls
    warmup._last_warm_at = None


def _apply_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(warmup, "get_settings", lambda: settings)


def test_warmup_disabled_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_settings(monkeypatch, _warm_settings(market_warmup_enabled=False))
    now = datetime(2026, 7, 15, 10, 0, tzinfo=SHANGHAI)
    assert warmup.warm_market_if_due(now) is False


def test_warmup_after_close_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_settings(monkeypatch, _warm_settings())
    now = datetime(2026, 7, 15, 16, 0, tzinfo=SHANGHAI)
    assert warmup.warm_market_if_due(now) is False


def test_warmup_weekend_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_settings(monkeypatch, _warm_settings())
    now = datetime(2026, 7, 18, 10, 0, tzinfo=SHANGHAI)
    assert warmup.warm_market_if_due(now) is False


def test_warmup_triggers_during_trading_hours(
    monkeypatch: pytest.MonkeyPatch, _reset_warmup: dict[str, int]
) -> None:
    _apply_settings(monkeypatch, _warm_settings())
    now = datetime(2026, 7, 15, 10, 0, tzinfo=SHANGHAI)
    assert warmup.warm_market_if_due(now) is True
    _wait_until(lambda: _reset_warmup["count"] == 1)


def test_warmup_interval_blocks_repeat(
    monkeypatch: pytest.MonkeyPatch, _reset_warmup: dict[str, int]
) -> None:
    _apply_settings(monkeypatch, _warm_settings(market_warmup_interval_minutes=5))
    now = datetime(2026, 7, 15, 10, 0, tzinfo=SHANGHAI)
    assert warmup.warm_market_if_due(now) is True
    assert warmup.warm_market_if_due(now + timedelta(minutes=1)) is False
    _wait_until(lambda: _reset_warmup["count"] == 1)
