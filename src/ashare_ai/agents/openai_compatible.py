"""OpenAI-compatible structured output client backed by the Responses API."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from ashare_ai.agents.protocols import GenerationMetadata, StructuredGeneration

_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", flags=re.DOTALL | re.IGNORECASE)
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})


class OpenAICompatibleError(RuntimeError):
    """Raised when a compatible Responses API cannot produce a valid response."""


class OpenAICompatibleStructuredLLMClient:
    """Generate Pydantic-validated JSON with an OpenAI-compatible Responses endpoint.

    The request uses the Responses API ``text.format`` JSON-schema contract.  Providers
    that implement the OpenAI-compatible endpoint therefore receive the same strict
    schema that is used by OpenAI's native API.
    """

    provider = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        reasoning_effort: str = "high",
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not api_key:
            raise ValueError("api_key must not be empty")
        if not model:
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be non-negative")

        normalized_url = base_url.rstrip("/")
        self._base_url = (
            normalized_url if normalized_url.endswith("/v1") else f"{normalized_url}/v1"
        )
        self._api_key = api_key
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._client = client

    async def generate_structured(
        self,
        *,
        schema: type[BaseModel],
        messages: tuple[Mapping[str, str], ...],
        idempotency_key: str,
    ) -> StructuredGeneration:
        if not messages:
            raise ValueError("messages must not be empty")
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")

        request_body = {
            "model": self._model,
            "input": _messages_to_input(messages),
            "reasoning": {"effort": self._reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": _schema_name(schema),
                    "strict": True,
                    "schema": _strict_json_schema(schema.model_json_schema()),
                }
            },
        }
        started = perf_counter()
        attempts = 0
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            while True:
                try:
                    response = await client.post(
                        f"{self._base_url}/responses",
                        json=request_body,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Idempotency-Key": idempotency_key,
                        },
                        timeout=self._timeout,
                    )
                    if response.status_code in _RETRYABLE_STATUS_CODES:
                        raise _RetryableResponseError(response)
                    if response.is_error:
                        raise OpenAICompatibleError(_http_error_message(response))
                    response_data = _decode_response(response)
                    parsed_output = _parse_output(_extract_output_text(response_data))
                    try:
                        validated = schema.model_validate(parsed_output)
                    except ValidationError as exc:
                        raise OpenAICompatibleError(
                            f"Responses API output does not satisfy {schema.__name__}: {exc}"
                        ) from exc
                    usage = _usage(response_data)
                    return StructuredGeneration(
                        output=validated.model_dump(mode="json"),
                        metadata=GenerationMetadata(
                            provider=self.provider,
                            model_name=str(response_data.get("model") or self._model),
                            reasoning_effort=self._reasoning_effort,
                            input_tokens=usage[0],
                            output_tokens=usage[1],
                            duration_ms=int((perf_counter() - started) * 1000),
                            retry_count=attempts,
                        ),
                    )
                except _RetryableResponseError as exc:
                    if attempts >= self._max_retries:
                        raise OpenAICompatibleError(_http_error_message(exc.response)) from exc
                except httpx.TransportError as exc:
                    if attempts >= self._max_retries:
                        raise OpenAICompatibleError(
                            f"Responses API transport failure: {exc}"
                        ) from exc
                attempts += 1
                if self._retry_backoff:
                    await asyncio.sleep(self._retry_backoff * (2 ** (attempts - 1)))
        finally:
            if owns_client:
                await client.aclose()


class _RetryableResponseError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response


def _messages_to_input(messages: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    input_messages: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role:
            raise ValueError(f"messages[{index}].role must be a non-empty string")
        if not isinstance(content, str):
            raise ValueError(f"messages[{index}].content must be a string")
        input_messages.append({"role": role, "content": [{"type": "input_text", "text": content}]})
    return input_messages


def _schema_name(schema: type[BaseModel]) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", schema.__name__).strip("_")
    return (normalized or "structured_response")[:64]


def _strict_json_schema(value: Any) -> Any:
    """Make every object schema compatible with strict Responses JSON-schema mode.

    Pydantic's default models permit undeclared properties.  Responses strict mode
    requires the explicit opposite on each object, including object definitions
    stored under ``$defs``.  References are preserved because only object schema
    nodes themselves are amended.
    """
    if isinstance(value, Mapping):
        normalized = {key: _strict_json_schema(nested) for key, nested in value.items()}
        if normalized.get("type") == "object" or "properties" in normalized:
            normalized["additionalProperties"] = False
            properties = normalized.get("properties")
            if isinstance(properties, Mapping):
                # Responses strict mode requires every declared property to be required.
                # Optional Pydantic fields retain their null/default-shaped schema, so the
                # model can return an explicit null or empty value where appropriate.
                normalized["required"] = list(properties)
        return normalized
    if isinstance(value, list):
        return [_strict_json_schema(item) for item in value]
    return value


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise OpenAICompatibleError("Responses API returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise OpenAICompatibleError("Responses API response must be a JSON object")
    return data


def _extract_output_text(response: Mapping[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = response.get("output")
    if isinstance(output, Sequence) and not isinstance(output, (str, bytes, bytearray)):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
                continue
            for part in content:
                if not isinstance(part, Mapping) or part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
                elif isinstance(text, Mapping) and isinstance(text.get("value"), str):
                    texts.append(text["value"])
        if texts:
            return "".join(texts)
    raise OpenAICompatibleError("Responses API response contains no output text")


def _parse_output(text: str) -> dict[str, Any]:
    cleaned = text.strip().lstrip("\ufeff")
    match = _CODE_FENCE.match(cleaned)
    if match:
        cleaned = match.group(1).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise OpenAICompatibleError("Responses API output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise OpenAICompatibleError("Responses API output must be a JSON object")
    return payload


def _usage(response: Mapping[str, Any]) -> tuple[int, int]:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return (0, 0)
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return (_non_negative_int(input_tokens), _non_negative_int(output_tokens))


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _http_error_message(response: httpx.Response) -> str:
    # Upstream bodies are untrusted and may reflect prompts, credentials, or
    # internal diagnostics. Persist and expose only the stable status code.
    return f"Responses API request failed with status {response.status_code}"
