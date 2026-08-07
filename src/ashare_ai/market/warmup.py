"""Server-side market-data warmup for fast first-page loads.

Runs inside the long-lived ``job-worker`` process during trading hours. It
collects the union of every user's watchlist and positions (bounded), then
pre-fetches quotes and daily klines into the shared market cache (local + Redis).
The API reads that cache on its first request, so opening the homepage does not
pay the provider cold-start latency, and no single user's device has to warm the
data before anyone else's dashboard can render.

Design notes:

* The fetch runs in a daemon thread so a slow warmup never blocks the worker's
  queue polling loop.
* A dedicated light ``MarketDataService`` with a Sina primary is used instead of
  the API's runtime-profile service: warmup never spawns a heavy AKShare
  subprocess and never contends with the API's provider slots.
* After close (and on weekends) warmup is gated off, matching the existing
  ``is_after_close`` reclamation boundary.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.core.config import get_settings
from ashare_ai.core.runtime_mode import is_after_close
from ashare_ai.core.time import SHANGHAI
from ashare_ai.market.service import MarketDataService, SinaMarketProvider
from ashare_ai.storage.models import UserAssetState

logger = logging.getLogger(__name__)

_WARM_GUARD = threading.Lock()
_last_warm_at: float | None = None
_warmup_market: MarketDataService | None = None


def collect_warmup_symbols(
    session: Session,
    *,
    max_symbols: int,
    extra_symbols: Sequence[str] = (),
) -> list[str]:
    """Union of every user's watchlist and positions, bounded and sorted.

    ``extra_symbols`` (e.g. an operator-provided index hot list) keep their given
    order first, so they are never truncated when user watchlists fill the bound.
    """

    extra: list[str] = []
    for item in extra_symbols:
        cleaned = item.strip() if isinstance(item, str) else ""
        if cleaned and cleaned not in extra:
            extra.append(cleaned)
    symbols: set[str] = set()
    rows = session.scalars(select(UserAssetState)).all()
    for row in rows:
        for symbol in _watchlist_symbols(row.watchlist):
            symbols.add(symbol)
        for symbol in _position_symbols(row.positions):
            symbols.add(symbol)
    ordered = list(extra)
    for symbol in sorted(symbols - set(extra)):
        if len(ordered) >= max_symbols:
            break
        ordered.append(symbol)
    return ordered[:max_symbols]


def _watchlist_symbols(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _position_symbols(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    symbols: list[str] = []
    for position in value:
        symbol = position.get("symbol") if isinstance(position, dict) else None
        if isinstance(symbol, str) and symbol.strip():
            symbols.append(symbol.strip())
    return symbols


def warm_market_if_due(now: datetime | None = None) -> bool:
    """Gate and trigger one warmup; the fetch itself runs in a background thread.

    Returns ``True`` when a warmup was triggered, ``False`` when gated off
    (disabled, after close, or already warmed within the configured interval).
    """

    settings = get_settings()
    if not settings.market_warmup_enabled:
        return False
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if is_after_close(current):
        return False
    interval_seconds = settings.market_warmup_interval_minutes * 60
    global _last_warm_at
    with _WARM_GUARD:
        if _last_warm_at is not None and time.monotonic() - _last_warm_at < interval_seconds:
            return False
        _last_warm_at = time.monotonic()
    threading.Thread(target=_warm_background, name="market-warmup", daemon=True).start()
    return True


def _warm_background() -> None:
    settings = get_settings()
    from ashare_ai.storage.database import SessionLocal

    try:
        with SessionLocal() as session:
            symbols = collect_warmup_symbols(
                session,
                max_symbols=settings.market_warmup_max_symbols,
                extra_symbols=_index_symbols(settings.market_warmup_index_symbols),
            )
    except Exception:
        logger.warning("market warmup symbol scan failed", exc_info=True)
        return
    if not symbols:
        return
    try:
        _market_service().prefetch(
            symbols,
            periods=["day"],
            limit=settings.market_warmup_kline_limit,
            include_quotes=True,
            adjustment="raw",
        )
    except Exception:
        logger.warning("market warmup fetch failed for %d symbols", len(symbols), exc_info=True)


def _index_symbols(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _market_service() -> MarketDataService:
    global _warmup_market
    if _warmup_market is None:
        _warmup_market = MarketDataService(
            primary=SinaMarketProvider(),
            settings=get_settings(),
        )
    return _warmup_market
