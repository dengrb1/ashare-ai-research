from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from ashare_ai.core.config import Settings
from ashare_ai.core.system_settings import get_effective_settings

_FORBIDDEN_ENGINES = {"baidu", "sogou"}
_ALLOWED_ENGINES = {"google", "bing", "duckduckgo"}


class SearXNGSearchClient:
    """Small read-only adapter around a configured SearXNG JSON endpoint."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_effective_settings()
        parsed = urlsplit(self.settings.searxng_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("SEARXNG_BASE_URL must be an HTTP(S) origin")
        self.base_url = self.settings.searxng_base_url.rstrip("/")

    def search_news(
        self,
        query: str,
        *,
        max_results: int | None = None,
        time_range: str = "month",
    ) -> list[dict[str, Any]]:
        """Return recent news results only.

        SearXNG is intentionally not a general retrieval tool for model prompts:
        the caller supplies a stock-specific query and this adapter asks only for
        the news category.  URL, engine, and result-size checks remain at this
        boundary so untrusted search payloads cannot grow a chat request freely.
        """
        normalized = " ".join(query.split())[:256]
        if not normalized:
            return []
        limit = min(max_results or self.settings.searxng_max_results, 5)
        allowed_range = time_range if time_range in {"day", "month", "year"} else "month"
        params = {
            "q": normalized,
            "format": "json",
            "language": "en-US",
            "categories": "news",
            "engines": "google,bing,duckduckgo",
            "time_range": allowed_range,
        }
        response = httpx.get(
            f"{self.base_url}/search",
            params=params,
            headers={
                "User-Agent": "AShareResearch/1.0",
                "X-Real-IP": "127.0.0.1",
                "X-Forwarded-For": "127.0.0.1",
            },
            timeout=self.settings.searxng_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if not rows:
            params.pop("engines")
            response = httpx.get(
                f"{self.base_url}/search",
                params=params,
                headers={
                    "User-Agent": "AShareResearch/1.0",
                    "X-Real-IP": "127.0.0.1",
                    "X-Forwarded-For": "127.0.0.1",
                },
                timeout=self.settings.searxng_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("results", []) if isinstance(payload, dict) else []
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            engine = str(row.get("engine") or "").casefold()
            url = str(row.get("url") or "")
            parsed = urlsplit(url)
            if (
                engine in _FORBIDDEN_ENGINES
                or (engine and engine not in _ALLOWED_ENGINES)
                or parsed.scheme not in {"http", "https"}
            ):
                continue
            if not parsed.hostname or url in seen:
                continue
            seen.add(url)
            results.append(
                {
                    "title": str(row.get("title") or parsed.hostname)[:300],
                    "url": url[:2048],
                    "snippet": str(row.get("content") or "")[:1200],
                    "engine": engine or "searxng",
                    "published_at": row.get("publishedDate"),
                }
            )
            if len(results) >= limit:
                break
        return results

    def search(self, query: str, *, max_results: int | None = None) -> list[dict[str, Any]]:
        """Search the public web with a bounded result and snippet budget."""
        normalized = " ".join(query.split())[:256]
        if not normalized:
            return []
        limit = min(max_results or self.settings.searxng_max_results, 5)
        params = {
            "q": normalized,
            "format": "json",
            "language": "en-US",
            "categories": "general",
            "engines": "google,bing,duckduckgo",
        }
        response = httpx.get(
            f"{self.base_url}/search",
            params=params,
            headers={"User-Agent": "AShareResearch/1.0"},
            timeout=self.settings.searxng_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            engine = str(row.get("engine") or "").casefold()
            url = str(row.get("url") or "")
            parsed = urlsplit(url)
            if engine not in _ALLOWED_ENGINES or parsed.scheme not in {"http", "https"}:
                continue
            if not parsed.hostname or url in seen:
                continue
            seen.add(url)
            results.append(
                {
                    "title": str(row.get("title") or parsed.hostname)[:300],
                    "url": url[:2048],
                    "snippet": str(row.get("content") or "")[:1200],
                    "engine": engine,
                    "published_at": row.get("publishedDate"),
                }
            )
            if len(results) >= limit:
                break
        return results

    def available(self) -> bool:
        try:
            response = httpx.get(f"{self.base_url}/healthz", timeout=3.0)
            return response.status_code < 500
        except httpx.HTTPError:
            return False
