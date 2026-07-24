from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import redis

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import sha256_bytes
from ashare_ai.search.searxng import SearXNGSearchClient

_REALTIME_TERMS = (
    "最新",
    "今日",
    "今天",
    "实时",
    "刚刚",
    "当前",
    "now",
    "today",
    "latest",
    "live",
    "breaking",
    "current",
)


@dataclass(frozen=True)
class WebSearchResult:
    items: list[dict[str, Any]]
    status: dict[str, Any]
    cache_hit: bool


class WebSearchService:
    """Public query cache keyed only by SHA-256, with Redis single-flight."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: SearXNGSearchClient | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or SearXNGSearchClient(self.settings)
        self.redis = redis_client or redis.Redis.from_url(
            self.settings.redis_url, decode_responses=True
        )

    def search(self, query: str) -> WebSearchResult:
        normalized = " ".join(query.split())[:256]
        if not normalized:
            return WebSearchResult([], {"state": "EMPTY", "source": "searxng"}, False)
        digest = sha256_bytes(normalized.casefold().encode())
        cache_key = f"ashare:web-search:v1:{digest}"
        lock_key = f"{cache_key}:lock"
        ttl = 300 if any(term in normalized.casefold() for term in _REALTIME_TERMS) else 1800
        try:
            cached = cast(str | None, self.redis.get(cache_key))
            if cached:
                payload = json.loads(cached)
                return WebSearchResult(
                    list(payload.get("items", [])), dict(payload.get("status", {})), True
                )
            owns_lock = bool(self.redis.set(lock_key, "1", nx=True, ex=15))
            if not owns_lock:
                for _ in range(20):
                    time.sleep(0.1)
                    cached = cast(str | None, self.redis.get(cache_key))
                    if cached:
                        payload = json.loads(cached)
                        return WebSearchResult(
                            list(payload.get("items", [])), dict(payload.get("status", {})), True
                        )
                return WebSearchResult(
                    [],
                    {
                        "state": "UNAVAILABLE",
                        "reason_code": "SEARCH_IN_PROGRESS",
                        "source": "searxng",
                    },
                    False,
                )
            try:
                items = self.client.search(normalized, max_results=5)
                status = {
                    "state": "AVAILABLE" if items else "EMPTY",
                    "reason_code": "OK" if items else "NO_RESULTS",
                    "source": "searxng",
                    "searched_at": datetime.now(UTC).isoformat(),
                    "cache_ttl_seconds": ttl,
                }
                self.redis.setex(cache_key, ttl, json.dumps({"items": items, "status": status}))
                return WebSearchResult(items, status, False)
            finally:
                self.redis.delete(lock_key)
        except (redis.RedisError, json.JSONDecodeError):
            try:
                items = self.client.search(normalized, max_results=5)
                return WebSearchResult(
                    items,
                    {
                        "state": "AVAILABLE" if items else "EMPTY",
                        "source": "searxng",
                        "cache": "DEGRADED",
                    },
                    False,
                )
            except Exception:
                return WebSearchResult(
                    [],
                    {
                        "state": "UNAVAILABLE",
                        "reason_code": "SEARCH_UPSTREAM_UNAVAILABLE",
                        "source": "searxng",
                    },
                    False,
                )
        except Exception:
            return WebSearchResult(
                [],
                {
                    "state": "UNAVAILABLE",
                    "reason_code": "SEARCH_UPSTREAM_UNAVAILABLE",
                    "source": "searxng",
                },
                False,
            )


_service: WebSearchService | None = None


def get_web_search_service() -> WebSearchService:
    global _service
    if _service is None:
        _service = WebSearchService()
    return _service
