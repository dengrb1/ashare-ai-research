"""Gateway client for model proxy service."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ashare_ai.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class GatewayClient:
    """HTTP client for Gateway service.

    Gateway is a Rust-based model proxy that routes requests to configured
    AI model providers. It provides a unified interface for model inference.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.gateway_url.rstrip("/")
        self.timeout = 60.0  # Model inference can take time
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
        """Check Gateway health status.

        Returns:
            True if service is healthy, False otherwise.
        """
        try:
            response = self.client.get("/health", timeout=2.0)
            return response.status_code == 200
        except Exception as exc:
            logger.debug("Gateway health check failed: %s", exc)
            return False

    def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Request chat completion from Gateway.

        Args:
            model: Model identifier (e.g., "gpt-4", "claude-3-opus")
            messages: List of message dicts with "role" and "content"
            temperature: Sampling temperature (0.0 - 2.0)
            max_tokens: Maximum tokens to generate
            stream: Whether to stream the response

        Returns:
            Completion response dictionary containing:
            - id: Completion ID
            - model: Model used
            - choices: List of completion choices
            - usage: Token usage stats

        Raises:
            httpx.HTTPError: On network or HTTP errors.
            ValueError: On invalid response format.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        try:
            response = self.client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Gateway returned invalid response format")

            return data
        except Exception as exc:
            logger.warning("Gateway chat_completion failed: %s", exc)
            raise

    def embeddings(
        self,
        *,
        model: str,
        input: str | list[str],
    ) -> dict[str, Any]:
        """Request embeddings from Gateway.

        Args:
            model: Model identifier (e.g., "text-embedding-3-small")
            input: Text or list of texts to embed

        Returns:
            Embeddings response dictionary containing:
            - data: List of embedding objects with "embedding" vectors
            - model: Model used
            - usage: Token usage stats

        Raises:
            httpx.HTTPError: On network or HTTP errors.
            ValueError: On invalid response format.
        """
        payload = {
            "model": model,
            "input": input,
        }

        try:
            response = self.client.post("/v1/embeddings", json=payload)
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Gateway returned invalid response format")

            return data
        except Exception as exc:
            logger.warning("Gateway embeddings failed: %s", exc)
            raise


def get_gateway_client(settings: Settings | None = None) -> GatewayClient:
    """Factory function for Gateway client."""
    return GatewayClient(settings=settings)
