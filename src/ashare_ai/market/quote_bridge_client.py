"""Quote Bridge client for real-time market data from Tencent/Sina public APIs."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ashare_ai.adapters.symbols import normalize_symbol
from ashare_ai.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class QuoteBridgeClient:
    """HTTP client for Quote Bridge service.

    Quote Bridge is a stdlib-only service that fetches real-time quotes from
    Tencent (primary) and Sina (fallback) public APIs. It serves as a supplementary
    data source for the market data layer.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.quote_bridge_url.rstrip("/")
        self.timeout = 5.0  # Quote Bridge should respond quickly
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
        """Check Quote Bridge health status.

        Returns:
            True if service is healthy, False otherwise.
        """
        try:
            response = self.client.get("/health", timeout=2.0)
            return response.status_code == 200
        except Exception as exc:
            logger.debug("Quote Bridge health check failed: %s", exc)
            return False

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        """Fetch a single quote from Quote Bridge.

        Args:
            symbol: Stock symbol (e.g., "000001.SZ", "600000.SS")

        Returns:
            Quote data dictionary, or None if unavailable.

        Raises:
            httpx.HTTPError: On network or HTTP errors.
            ValueError: On invalid response format.
        """
        normalized = str(normalize_symbol(symbol))
        try:
            response = self.client.get(f"/quote", params={"symbol": normalized})
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Quote Bridge returned invalid response format")

            # Bridge returns {"symbol": "...", "price": ..., "timestamp": "...", ...}
            # Transform to match MarketDataService quote format
            return self._normalize_quote(data)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.debug("Quote Bridge: symbol not found: %s", normalized)
                return None
            raise
        except Exception as exc:
            logger.warning("Quote Bridge get_quote failed for %s: %s", normalized, exc)
            raise

    def get_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Fetch multiple quotes from Quote Bridge.

        Args:
            symbols: List of stock symbols

        Returns:
            List of quote data dictionaries (may be partial if some symbols fail).

        Raises:
            httpx.HTTPError: On network or HTTP errors.
            ValueError: On invalid response format.
        """
        if not symbols:
            return []

        normalized = [str(normalize_symbol(sym)) for sym in symbols]
        try:
            response = self.client.get(
                "/quote",
                params={"symbols": ",".join(normalized)}
            )
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, list):
                raise ValueError("Quote Bridge returned invalid response format")

            return [self._normalize_quote(item) for item in data if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("Quote Bridge get_quotes failed: %s", exc)
            raise

    def _normalize_quote(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Quote Bridge response to MarketDataService format.

        Bridge format:
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "price": 13.45,
            "change": 0.12,
            "change_percent": 0.90,
            "open": 13.33,
            "high": 13.50,
            "low": 13.30,
            "previous_close": 13.33,
            "volume": 123456789,
            "amount": 1234567890.0,
            "timestamp": "2026-08-26T15:00:00+08:00"
        }
        """
        return {
            "symbol": data.get("symbol"),
            "name": data.get("name"),
            "price": self._safe_float(data.get("price")),
            "change": self._safe_float(data.get("change")),
            "change_percent": self._safe_float(data.get("change_percent")),
            "open": self._safe_float(data.get("open")),
            "high": self._safe_float(data.get("high")),
            "low": self._safe_float(data.get("low")),
            "previous_close": self._safe_float(data.get("previous_close")),
            "volume": self._safe_float(data.get("volume")),
            "amount": self._safe_float(data.get("amount")),
            "_trading_at": data.get("timestamp"),  # ISO format timestamp
        }

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """Convert value to float, return None if invalid."""
        if value is None or value in {"", "-", "--"}:
            return None
        try:
            result = float(value)
            return result if result != float("inf") and result != float("-inf") else None
        except (TypeError, ValueError):
            return None


def get_quote_bridge_client(settings: Settings | None = None) -> QuoteBridgeClient:
    """Factory function for Quote Bridge client."""
    return QuoteBridgeClient(settings=settings)
