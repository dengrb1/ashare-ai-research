from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict

from ashare_ai.agents.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleStructuredLLMClient,
)


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation: str
    confidence: float


class _NestedResult(BaseModel):
    rationale: str


class _ResultWithReference(BaseModel):
    nested: _NestedResult


@pytest.mark.asyncio
@respx.mock
async def test_generate_structured_posts_strict_schema_and_normalizes_code_fence() -> None:
    route = respx.post("http://llm.local/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "gpt-5.6-sol",
                "usage": {"input_tokens": 12, "output_tokens": 7},
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '```json\n{"recommendation": "hold", "confidence": 0.7}\n```'
                                ),
                            }
                        ],
                    }
                ],
            },
        )
    )
    client = OpenAICompatibleStructuredLLMClient(
        base_url="http://llm.local",
        api_key="secret",
        model="configured-model",
    )

    generation = await client.generate_structured(
        schema=_Result,
        messages=(
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Assess."},
        ),
        idempotency_key="request-123",
    )

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer secret"
    assert request.headers["Idempotency-Key"] == "request-123"
    request_json = request.content.decode()
    assert '"strict":true' in request_json
    assert '"type":"json_schema"' in request_json
    assert generation.output == {"recommendation": "hold", "confidence": 0.7}
    assert generation.metadata.model_name == "gpt-5.6-sol"
    assert generation.metadata.input_tokens == 12
    assert generation.metadata.output_tokens == 7
    assert generation.metadata.retry_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_generate_structured_retries_transient_failure_with_same_idempotency_key() -> None:
    route = respx.post("http://llm.local/v1/responses").mock(
        side_effect=[
            httpx.Response(503, text="temporarily unavailable"),
            httpx.Response(
                200,
                json={
                    "output_text": '{"recommendation": "buy", "confidence": 0.8}',
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
            ),
        ]
    )
    client = OpenAICompatibleStructuredLLMClient(
        base_url="http://llm.local/v1/",
        api_key="secret",
        model="configured-model",
        max_retries=1,
        retry_backoff=0,
    )

    generation = await client.generate_structured(
        schema=_Result,
        messages=({"role": "user", "content": "Assess."},),
        idempotency_key="stable-key",
    )

    assert route.call_count == 2
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        "stable-key",
        "stable-key",
    ]
    assert generation.metadata.retry_count == 1
    assert generation.metadata.model_name == "configured-model"


@pytest.mark.asyncio
@respx.mock
async def test_generate_structured_normalizes_object_schemas_without_expanding_references() -> None:
    route = respx.post("http://llm.local/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={"output_text": '{"nested": {"rationale": "evidence"}}'},
        )
    )
    client = OpenAICompatibleStructuredLLMClient(
        base_url="http://llm.local/v1",
        api_key="secret",
        model="configured-model",
    )

    await client.generate_structured(
        schema=_ResultWithReference,
        messages=({"role": "user", "content": "Assess."},),
        idempotency_key="schema-key",
    )

    request_schema = json.loads(route.calls.last.request.content)["text"]["format"]["schema"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["required"]) == set(request_schema["properties"])
    assert request_schema["$defs"]["_NestedResult"]["additionalProperties"] is False
    assert set(request_schema["$defs"]["_NestedResult"]["required"]) == set(
        request_schema["$defs"]["_NestedResult"]["properties"]
    )
    assert request_schema["properties"]["nested"] == {"$ref": "#/$defs/_NestedResult"}


@pytest.mark.asyncio
@respx.mock
async def test_generate_structured_surfaces_invalid_json_without_fallback() -> None:
    respx.post("http://llm.local/v1/responses").mock(
        return_value=httpx.Response(200, json={"output_text": "not JSON"})
    )
    client = OpenAICompatibleStructuredLLMClient(
        base_url="http://llm.local/v1",
        api_key="secret",
        model="configured-model",
    )

    with pytest.raises(OpenAICompatibleError, match="not valid JSON"):
        await client.generate_structured(
            schema=_Result,
            messages=({"role": "user", "content": "Assess."},),
            idempotency_key="key",
        )
