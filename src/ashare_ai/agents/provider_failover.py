"""Ordered, no-fan-out fallback for OpenAI-compatible model providers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from ashare_ai.agents.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleStructuredLLMClient,
)
from ashare_ai.agents.protocols import StructuredGeneration


@dataclass(frozen=True)
class ProviderClient:
    provider_id: str
    client: OpenAICompatibleStructuredLLMClient


class ProviderFailoverStructuredLLMClient:
    """Try independent providers in priority order for recoverable failures only."""

    provider = "openai-compatible-fallback"

    def __init__(self, providers: Sequence[ProviderClient]) -> None:
        if not providers:
            raise ValueError("at least one provider client is required")
        self._providers = tuple(providers)
        self.last_provider_id = self._providers[0].provider_id
        self.last_protocol = "RESPONSES"
        self.last_endpoint_path = "/responses"
        self.last_request_mode = "json_schema"
        self.last_status_code: int | None = None

    async def generate_structured(
        self,
        *,
        schema: type[BaseModel],
        messages: tuple[Mapping[str, str], ...],
        idempotency_key: str,
    ) -> StructuredGeneration:
        last_error: Exception | None = None
        for index, provider in enumerate(self._providers):
            try:
                result = await provider.client.generate_structured(
                    schema=schema,
                    messages=messages,
                    idempotency_key=idempotency_key,
                )
                self._capture(provider)
                return result.model_copy(
                    update={
                        "metadata": result.metadata.model_copy(
                            update={"provider": f"openai-compatible:{provider.provider_id}"}
                        )
                    }
                )
            except (OpenAICompatibleError, httpx.HTTPError) as exc:
                self._capture(provider)
                last_error = exc
                if index == len(self._providers) - 1 or not _is_failover_error(exc):
                    raise
        assert last_error is not None
        raise last_error

    async def stream_text(
        self,
        *,
        messages: tuple[Mapping[str, str], ...],
        idempotency_key: str,
        previous_response_id: str | None = None,
        prompt_cache_key: str | None = None,
        allow_degraded: bool = True,
        json_object: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        last_error: Exception | None = None
        for index, provider in enumerate(self._providers):
            emitted_text = False
            try:
                async for event in provider.client.stream_text(
                    messages=messages,
                    idempotency_key=idempotency_key,
                    previous_response_id=(previous_response_id if index == 0 else None),
                    prompt_cache_key=prompt_cache_key,
                    allow_degraded=allow_degraded,
                    json_object=json_object,
                ):
                    if event.get("type") == "delta" and event.get("delta"):
                        emitted_text = True
                    if event.get("type") == "completed":
                        event = {
                            **event,
                            "provider": f"openai-compatible:{provider.provider_id}",
                        }
                    yield event
                self._capture(provider)
                return
            except (OpenAICompatibleError, httpx.HTTPError) as exc:
                self._capture(provider)
                last_error = exc
                if emitted_text or index == len(self._providers) - 1 or not _is_failover_error(exc):
                    raise
        assert last_error is not None
        raise last_error

    async def probe_stream(self) -> bool:
        async for event in self.stream_text(
            messages=({"role": "user", "content": "Reply with ok."},),
            idempotency_key=f"stream-probe-{self.last_provider_id}",
            allow_degraded=False,
        ):
            if event.get("type") == "completed":
                return True
        return False

    def _capture(self, provider: ProviderClient) -> None:
        self.last_provider_id = provider.provider_id
        self.last_protocol = provider.client.last_protocol
        self.last_endpoint_path = provider.client.last_endpoint_path
        self.last_request_mode = provider.client.last_request_mode
        self.last_status_code = provider.client.last_status_code


def _is_failover_error(exc: OpenAICompatibleError | httpx.HTTPError) -> bool:
    if isinstance(exc, OpenAICompatibleError):
        return exc.retryable or exc.code in {
            "MODEL_TIMEOUT",
            "MODEL_RATE_LIMITED",
            "MODEL_GATEWAY_UNAVAILABLE",
            "MODEL_STREAM_INCOMPLETE",
        }
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError))
