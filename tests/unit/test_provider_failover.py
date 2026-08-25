from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from pydantic import BaseModel

from ashare_ai.agents.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleStructuredLLMClient,
)
from ashare_ai.agents.protocols import GenerationMetadata, StructuredGeneration
from ashare_ai.agents.provider_failover import ProviderClient, ProviderFailoverStructuredLLMClient


class Probe(BaseModel):
    ok: bool


class FakeClient:
    last_protocol = "RESPONSES"
    last_endpoint_path = "/responses"
    last_request_mode = "json_schema"
    last_status_code = 503

    def __init__(self, *, outcome: str) -> None:
        self.outcome = outcome

    async def generate_structured(self, **_kwargs):
        if self.outcome == "fail":
            raise OpenAICompatibleError(
                "relay unavailable", code="MODEL_GATEWAY_UNAVAILABLE", retryable=True
            )
        return StructuredGeneration(
            output={"ok": True},
            metadata=GenerationMetadata(
                provider="openai-compatible", model_name="fallback-model", reasoning_effort="low",
                input_tokens=1, output_tokens=1, duration_ms=1, retry_count=0,
            ),
        )

    async def stream_text(self, **_kwargs):
        if self.outcome == "fail":
            raise OpenAICompatibleError(
                "relay timeout", code="MODEL_TIMEOUT", retryable=True
            )
        yield {"type": "delta", "delta": "ok"}
        yield {"type": "completed", "model": "fallback-model"}


def test_structured_generation_moves_to_the_next_provider() -> None:
    client = ProviderFailoverStructuredLLMClient(
        [
            ProviderClient("primary", FakeClient(outcome="fail")),  # type: ignore[arg-type]
            ProviderClient("fallback", FakeClient(outcome="success")),  # type: ignore[arg-type]
        ]
    )

    result = asyncio.run(
        client.generate_structured(
            schema=Probe,
            messages=({"role": "user", "content": "ping"},),
            idempotency_key="request-1",
        )
    )

    assert result.output == {"ok": True}
    assert result.metadata.provider == "openai-compatible:fallback"
    assert client.last_provider_id == "fallback"


@pytest.mark.asyncio
async def test_streaming_fallback_only_happens_before_text_is_emitted() -> None:
    client = ProviderFailoverStructuredLLMClient(
        [
            ProviderClient("primary", FakeClient(outcome="fail")),  # type: ignore[arg-type]
            ProviderClient("fallback", FakeClient(outcome="success")),  # type: ignore[arg-type]
        ]
    )
    events = [
        event
        async for event in client.stream_text(
            messages=({"role": "user", "content": "ping"},),
            idempotency_key="request-2",
        )
    ]

    assert [event["type"] for event in events] == ["delta", "completed"]
    assert events[-1]["provider"] == "openai-compatible:fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 503])
@respx.mock
async def test_exhausted_retryable_structured_responses_fail_over(status_code: int) -> None:
    primary_route = respx.post("https://primary.example/v1/responses").mock(
        return_value=httpx.Response(status_code)
    )
    fallback_route = respx.post("https://fallback.example/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={"model": "fallback-model", "output_text": '{"ok": true}'},
        )
    )
    client = ProviderFailoverStructuredLLMClient(
        [
            ProviderClient(
                "primary",
                OpenAICompatibleStructuredLLMClient(
                    base_url="https://primary.example",
                    api_key="primary-key",
                    model="primary-model",
                    max_retries=0,
                ),
            ),
            ProviderClient(
                "fallback",
                OpenAICompatibleStructuredLLMClient(
                    base_url="https://fallback.example",
                    api_key="fallback-key",
                    model="fallback-model",
                    max_retries=0,
                ),
            ),
        ]
    )

    result = await client.generate_structured(
        schema=Probe,
        messages=({"role": "user", "content": "ping"},),
        idempotency_key="request-retryable",
    )

    assert result.output == {"ok": True}
    assert primary_route.call_count == 1
    assert fallback_route.call_count == 1
    assert client.last_provider_id == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [httpx.ReadTimeout("relay timed out"), httpx.ConnectError("relay unavailable")],
)
@respx.mock
async def test_exhausted_transport_error_structured_response_fails_over(
    transport_error: httpx.TransportError,
) -> None:
    primary_route = respx.post("https://primary.example/v1/responses").mock(
        side_effect=transport_error
    )
    fallback_route = respx.post("https://fallback.example/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={"model": "fallback-model", "output_text": '{"ok": true}'},
        )
    )
    client = ProviderFailoverStructuredLLMClient(
        [
            ProviderClient(
                "primary",
                OpenAICompatibleStructuredLLMClient(
                    base_url="https://primary.example",
                    api_key="primary-key",
                    model="primary-model",
                    max_retries=0,
                ),
            ),
            ProviderClient(
                "fallback",
                OpenAICompatibleStructuredLLMClient(
                    base_url="https://fallback.example",
                    api_key="fallback-key",
                    model="fallback-model",
                    max_retries=0,
                ),
            ),
        ]
    )

    result = await client.generate_structured(
        schema=Probe,
        messages=({"role": "user", "content": "ping"},),
        idempotency_key="request-transport",
    )

    assert result.output == {"ok": True}
    assert primary_route.call_count == 1
    assert fallback_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_non_retryable_structured_response_does_not_fail_over() -> None:
    primary_route = respx.post("https://primary.example/v1/responses").mock(
        side_effect=[
            httpx.Response(400),
            httpx.Response(400),
        ]
    )
    chat_route = respx.post("https://primary.example/v1/chat/completions").mock(
        return_value=httpx.Response(400)
    )
    fallback_route = respx.post("https://fallback.example/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={"model": "fallback-model", "output_text": '{"ok": true}'},
        )
    )
    client = ProviderFailoverStructuredLLMClient(
        [
            ProviderClient(
                "primary",
                OpenAICompatibleStructuredLLMClient(
                    base_url="https://primary.example",
                    api_key="primary-key",
                    model="primary-model",
                    max_retries=0,
                ),
            ),
            ProviderClient(
                "fallback",
                OpenAICompatibleStructuredLLMClient(
                    base_url="https://fallback.example",
                    api_key="fallback-key",
                    model="fallback-model",
                    max_retries=0,
                ),
            ),
        ]
    )

    with pytest.raises(OpenAICompatibleError) as caught:
        await client.generate_structured(
            schema=Probe,
            messages=({"role": "user", "content": "ping"},),
            idempotency_key="request-invalid",
        )

    assert caught.value.status_code == 400
    assert caught.value.retryable is False
    assert primary_route.call_count == 2
    assert chat_route.call_count == 1
    assert fallback_route.call_count == 0
