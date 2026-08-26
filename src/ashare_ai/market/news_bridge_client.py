"""News Bridge client for news data from Eastmoney API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ashare_ai.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class NewsBridgeClient:
    """HTTP client for News Bridge service.

    News Bridge is a stdlib-only service that fetches news data from Eastmoney
    public API. It serves as a supplementary data source for research workflows.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.news_bridge_url.rstrip("/")
        self.timeout = 10.0  # News queries may take longer
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        """Lazy HTTP client initialization."""
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def health(self) -> bool:
        """Check News Bridge health status.

        Returns:
            True if service is healthy, False otherwise.
        """
        try:
            response = self.client.get("/health", timeout=2.0)
            return response.status_code == 200
        except Exception as exc:
            logger.debug("News Bridge health check failed: %s", exc)
            return False

    def get_news(
        self,
        *,
        symbol: str | None = None,
        category: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch news articles from News Bridge.

        Args:
            symbol: Optional stock symbol to filter news (e.g., "000001.SZ")
            category: Optional news category filter
            limit: Maximum number of articles to return

        Returns:
            List of news article dictionaries, each containing:
            - title: Article title
            - summary: Brief summary
            - content: Full content (if available)
            - source: News source
            - published_at: ISO timestamp
            - url: Article URL
            - symbols: Related stock symbols (list)

        Raises:
            httpx.HTTPError: On network or HTTP errors.
            ValueError: On invalid response format.
        """
        params: dict[str, Any] = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if category:
            params["category"] = category

        try:
            response = self.client.get("/news", params=params)
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, list):
                raise ValueError("News Bridge returned invalid response format")

            return [self._normalize_news(item) for item in data if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("News Bridge get_news failed: %s", exc)
            raise

    def _normalize_news(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize News Bridge response format.

        Bridge format:
        {
            "title": "...",
            "summary": "...",
            "content": "...",
            "source": "Eastmoney",
            "published_at": "2026-08-26T10:30:00+08:00",
            "url": "https://...",
            "symbols": ["000001.SZ", ...]
        }
        """
        return {
            "title": data.get("title", ""),
            "summary": data.get("summary", ""),
            "content": data.get("content", ""),
            "source": data.get("source", ""),
            "published_at": data.get("published_at"),
            "url": data.get("url", ""),
            "symbols": data.get("symbols", []),
        }


def get_news_bridge_client(settings: Settings | None = None) -> NewsBridgeClient:
    """Factory function for News Bridge client."""
    return NewsBridgeClient(settings=settings)
