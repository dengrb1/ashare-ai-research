from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.search.searxng import SearXNGSearchClient


@dataclass(frozen=True)
class NewsSearchResult:
    items: list[dict[str, Any]]
    status: dict[str, Any]
    cache_hit: bool
    singleflight_wait_ms: int = 0


class NewsSearchService:
    """Bounded, target-aware SearXNG news retrieval with an in-process single flight."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: SearXNGSearchClient | None = None,
        cache_seconds: int = 300,
        max_entries: int = 256,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or SearXNGSearchClient(self.settings)
        self.cache_seconds = max(15, cache_seconds)
        self.max_entries = max(8, max_entries)
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, NewsSearchResult]] = OrderedDict()
        self._inflight: dict[str, threading.Event] = {}

    def search_for_security(
        self,
        *,
        symbol: str,
        name: str,
        max_results: int = 5,
        window_days: int = 30,
    ) -> NewsSearchResult:
        code = symbol.split(".", 1)[0]
        clean_name = "".join(name.split())
        key = f"{symbol}:{clean_name}:{min(max_results, 5)}:{window_days}"
        started = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > time.monotonic():
                self._cache.move_to_end(key)
                return NewsSearchResult(
                    items=[dict(item) for item in cached[1].items],
                    status=dict(cached[1].status),
                    cache_hit=True,
                )
            waiter = self._inflight.get(key)
            if waiter is None:
                waiter = threading.Event()
                self._inflight[key] = waiter
                owner = True
            else:
                owner = False
        if not owner:
            waiter.wait(timeout=self.settings.searxng_timeout_seconds + 2)
            waited = round((time.monotonic() - started) * 1000)
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None and cached[0] > time.monotonic():
                    self._cache.move_to_end(key)
                    return NewsSearchResult(
                        items=[dict(item) for item in cached[1].items],
                        status=dict(cached[1].status),
                        cache_hit=True,
                        singleflight_wait_ms=waited,
                    )
            return NewsSearchResult(
                items=[],
                status={
                    "state": "UNAVAILABLE",
                    "reason_code": "NEWS_SINGLEFLIGHT_TIMEOUT",
                    "source": "searxng",
                },
                cache_hit=False,
                singleflight_wait_ms=waited,
            )
        try:
            query = f"{clean_name or symbol} {code} 新闻 公告 行业快讯"
            raw_items = self.client.search_news(
                query,
                max_results=min(max_results, 5),
                time_range="day" if window_days <= 7 else "month",
            )
            items = _relevant_unique(raw_items, name=clean_name, code=code, limit=max_results)
            result = NewsSearchResult(
                items=items,
                status={
                    "state": "AVAILABLE" if items else "EMPTY",
                    "reason_code": "OK" if items else "NO_RELEVANT_NEWS",
                    "source": "searxng",
                    "query_window_days": window_days,
                    "searched_at": datetime.now(UTC).isoformat(),
                },
                cache_hit=False,
            )
            with self._lock:
                self._cache[key] = (time.monotonic() + self.cache_seconds, result)
                self._cache.move_to_end(key)
                while len(self._cache) > self.max_entries:
                    self._cache.popitem(last=False)
            return result
        except Exception:
            # Do not cache transport or parsing failures. A fresh request may recover.
            return NewsSearchResult(
                items=[],
                status={
                    "state": "UNAVAILABLE",
                    "reason_code": "NEWS_UPSTREAM_UNAVAILABLE",
                    "source": "searxng",
                },
                cache_hit=False,
            )
        finally:
            with self._lock:
                completion = self._inflight.pop(key, None)
                if completion is not None:
                    completion.set()


def _relevant_unique(
    rows: list[dict[str, Any]], *, name: str, code: str, limit: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    normalized_name = name.casefold()
    for row in rows:
        title = str(row.get("title") or "").strip()
        snippet = str(row.get("snippet") or "").strip()
        haystack = f"{title} {snippet}".casefold()
        if not ((normalized_name and normalized_name in haystack) or code in haystack):
            continue
        url = _canonical_url(str(row.get("url") or ""))
        if not url:
            continue
        dedupe = f"{url.casefold()}:{title.casefold()}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        results.append(
            {
                "title": title[:300],
                "url": url,
                "snippet": snippet[:1200],
                "engine": str(row.get("engine") or "searxng")[:64],
                "published_at": row.get("published_at"),
            }
        )
        if len(results) >= min(max(1, limit), 5):
            break
    return results


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    pairs = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.casefold().startswith(("utm_", "spm", "from"))
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), ""))[:2048]
