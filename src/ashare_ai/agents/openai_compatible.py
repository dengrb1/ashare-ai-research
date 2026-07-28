"""OpenAI-compatible structured output client backed by the Responses API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from time import perf_counter
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from ashare_ai.agents.protocols import GenerationMetadata, StructuredGeneration, TextGeneration

_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", flags=re.DOTALL | re.IGNORECASE)
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})
_STREAM_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_CACHE_POLICIES = frozenset({"GROK", "OPENAI", "COMPATIBLE"})
CachePolicy = Literal["GROK", "OPENAI", "COMPATIBLE"]


class ModelUsage(tuple[int, int, int, int, int]):
    """Normalized supplier usage: input, cached read, cache write, output, reasoning."""

    __slots__ = ()

    @property
    def input_tokens(self) -> int:
        return self[0]

    @property
    def cached_input_tokens(self) -> int:
        return self[1]

    @property
    def cache_write_tokens(self) -> int:
        return self[2]

    @property
    def output_tokens(self) -> int:
        return self[3]

    @property
    def reasoning_tokens(self) -> int:
        return self[4]


class OpenAICompatibleError(RuntimeError):
    """Raised when a compatible Responses API cannot produce a valid response."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "MODEL_RESPONSE_ERROR",
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


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
        cache_policy: CachePolicy = "COMPATIBLE",
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
        if cache_policy not in _CACHE_POLICIES:
            raise ValueError("cache_policy must be GROK, OPENAI or COMPATIBLE")

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
        self._cache_policy = cache_policy
        self._client = client

    async def generate_structured(
        self,
        *,
        schema: type[BaseModel],
        messages: tuple[Mapping[str, Any], ...],
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
        schema_fallback_used = False
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
                        error = OpenAICompatibleError(
                            _http_error_message(response),
                            code=_status_error_code(response.status_code),
                            status_code=response.status_code,
                        )
                        if (
                            not schema_fallback_used
                            and response.status_code in {400, 404, 422}
                        ):
                            # A number of OpenAI-compatible gateways implement the
                            # Responses API but only accept JSON Object mode.  The
                            # response is still parsed and validated against the
                            # Pydantic schema below, so this relaxes only the wire
                            # contract and not the application's output contract.
                            request_body["text"] = {"format": {"type": "json_object"}}
                            schema_fallback_used = True
                            continue
                        raise error
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
                            input_tokens=usage.input_tokens,
                            cached_input_tokens=usage.cached_input_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            output_tokens=usage.output_tokens,
                            reasoning_tokens=usage.reasoning_tokens,
                            cache_policy=self._cache_policy,
                            duration_ms=int((perf_counter() - started) * 1000),
                            retry_count=attempts,
                        ),
                    )
                except _RetryableResponseError as exc:
                    if attempts >= self._max_retries:
                        raise OpenAICompatibleError(_http_error_message(exc.response)) from exc
                except httpx.TimeoutException as exc:
                    if attempts >= self._max_retries:
                        raise OpenAICompatibleError(
                            "Responses API request timed out", code="MODEL_TIMEOUT", retryable=True
                        ) from exc
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

    async def generate_structured_from_stream(
        self,
        *,
        schema: type[BaseModel],
        messages: tuple[Mapping[str, Any], ...],
        idempotency_key: str,
    ) -> StructuredGeneration:
        """Collect a Responses text stream and validate its JSON output locally.

        This is for compatible gateways whose streaming Responses endpoint is
        reliable but whose synchronous strict JSON-schema endpoint is not.  The
        wire contract is text-only; the application contract remains the given
        Pydantic schema.
        """
        generation = await self.generate_json_object_from_stream(
            messages=messages, idempotency_key=idempotency_key
        )
        try:
            parsed_output = _parse_output(generation.text)
        except OpenAICompatibleError as exc:
            raise OpenAICompatibleError(
                "Responses API stream output is not valid JSON",
                code="MODEL_OUTPUT_INVALID_JSON",
            ) from exc
        try:
            validated = schema.model_validate(parsed_output)
        except ValidationError as exc:
            raise OpenAICompatibleError(
                f"Responses API stream output does not satisfy {schema.__name__}",
                code="MODEL_OUTPUT_SCHEMA_INVALID",
            ) from exc
        return StructuredGeneration(
            output=validated.model_dump(mode="json"),
            metadata=generation.metadata,
        )

    async def generate_json_object_from_stream(
        self,
        *,
        messages: tuple[Mapping[str, Any], ...],
        idempotency_key: str,
    ) -> TextGeneration:
        """Collect a JSON-object-mode Responses stream without imposing a schema.

        Some compatible gateways reliably support streaming JSON Object mode but
        reject strict JSON Schema.  Callers must still parse and validate the
        returned text in their own domain contract.
        """
        started = perf_counter()
        chunks: list[str] = []
        completed: Mapping[str, Any] | None = None
        async for event in self.stream_text(
            messages=messages,
            idempotency_key=idempotency_key,
            allow_degraded=True,
            json_object=True,
        ):
            if event.get("type") == "delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
            elif event.get("type") == "completed":
                completed = event
        if completed is None:
            raise OpenAICompatibleError(
                "Responses API stream did not complete", code="MODEL_STREAM_INCOMPLETE"
            )
        text = "".join(chunks).strip()
        if not text:
            raise OpenAICompatibleError("Responses API stream contains no text")
        return TextGeneration(
            text=text,
            metadata=GenerationMetadata(
                provider=self.provider,
                model_name=str(completed.get("model") or self._model),
                reasoning_effort=self._reasoning_effort,
                input_tokens=_non_negative_int(completed.get("input_tokens")),
                cached_input_tokens=_non_negative_int(completed.get("cached_input_tokens")),
                cache_write_tokens=_non_negative_int(completed.get("cache_write_tokens")),
                output_tokens=_non_negative_int(completed.get("output_tokens")),
                reasoning_tokens=_non_negative_int(completed.get("reasoning_tokens")),
                cache_policy=self._cache_policy,
                duration_ms=int((perf_counter() - started) * 1000),
                retry_count=0,
            ),
        )

    async def stream_text(
        self,
        *,
        messages: tuple[Mapping[str, Any], ...],
        idempotency_key: str,
        previous_response_id: str | None = None,
        prompt_cache_key: str | None = None,
        allow_degraded: bool = True,
        json_object: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized Responses API text deltas and a final usage event."""
        if not messages or not idempotency_key:
            raise ValueError("messages and idempotency_key are required")
        request_body = {
            "model": self._model,
            "input": _messages_to_input(messages),
            "reasoning": {"effort": self._reasoning_effort},
            "stream": True,
        }
        if json_object:
            request_body["text"] = {"format": {"type": "json_object"}}
        advanced_controls = self._cache_policy == "OPENAI"
        if advanced_controls and previous_response_id:
            request_body["previous_response_id"] = previous_response_id
        if advanced_controls:
            request_body["prompt_cache_key"] = _prompt_cache_key(prompt_cache_key, messages)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        attempts = 0
        emitted_text = False
        advanced_fallback_used = False
        try:
            while True:
                try:
                    completed_received = False
                    async with client.stream(
                        "POST",
                        f"{self._base_url}/responses",
                        json=request_body,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Idempotency-Key": idempotency_key,
                            "Accept": "text/event-stream",
                        },
                        timeout=self._timeout,
                    ) as response:
                        if response.status_code in _STREAM_RETRYABLE_STATUS_CODES:
                            raise _RetryableResponseError(response)
                        if response.is_error:
                            raise OpenAICompatibleError(
                                _http_error_message(response),
                                code=_status_error_code(response.status_code),
                                status_code=response.status_code,
                            )
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if not raw or raw == "[DONE]":
                                continue
                            try:
                                event = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            event_type = str(event.get("type") or "")
                            if event_type == "response.output_text.delta":
                                delta = event.get("delta")
                                if isinstance(delta, str) and delta:
                                    emitted_text = True
                                    yield {"type": "delta", "delta": delta}
                            elif event_type == "response.completed":
                                completed_received = True
                                completed = event.get("response")
                                usage = (
                                    completed.get("usage", {})
                                    if isinstance(completed, dict)
                                    else {}
                                )
                                normalized_completed = {
                                    "type": "completed",
                                    "model": (
                                        str(completed.get("model") or self._model)
                                        if isinstance(completed, dict)
                                        else self._model
                                    ),
                                    "input_tokens": _usage(usage).input_tokens,
                                    "cached_input_tokens": _usage(usage).cached_input_tokens,
                                    "cache_write_tokens": _usage(usage).cache_write_tokens,
                                    "output_tokens": _usage(usage).output_tokens,
                                    "reasoning_tokens": _usage(usage).reasoning_tokens,
                                    "cache_policy": self._cache_policy,
                                }
                                if isinstance(completed, dict) and isinstance(
                                    completed.get("id"), str
                                ):
                                    normalized_completed["response_id"] = str(completed["id"])
                                yield normalized_completed
                            elif event_type in {"response.failed", "error"}:
                                raise OpenAICompatibleError(
                                    "Responses API streaming generation failed",
                                    code="MODEL_RESPONSE_ERROR",
                                )
                    if not completed_received:
                        raise OpenAICompatibleError(
                            "Responses API stream ended before response.completed",
                            code="MODEL_STREAM_INCOMPLETE",
                            retryable=not emitted_text,
                        )
                    return
                except _RetryableResponseError as exc:
                    if emitted_text or attempts >= self._max_retries:
                        raise OpenAICompatibleError(
                            _http_error_message(exc.response),
                            code=_status_error_code(exc.response.status_code),
                            status_code=exc.response.status_code,
                            retryable=not emitted_text,
                        ) from exc
                except httpx.TimeoutException as exc:
                    if emitted_text or attempts >= self._max_retries:
                        raise OpenAICompatibleError(
                            "Responses API streaming request timed out",
                            code="MODEL_TIMEOUT",
                            retryable=not emitted_text,
                        ) from exc
                except httpx.TransportError as exc:
                    if emitted_text or attempts >= self._max_retries:
                        raise OpenAICompatibleError(
                            "Responses API transport failure",
                            code="MODEL_GATEWAY_UNAVAILABLE",
                            retryable=not emitted_text,
                        ) from exc
                except OpenAICompatibleError as exc:
                    if (
                        advanced_controls
                        and not advanced_fallback_used
                        and not emitted_text
                        and exc.status_code in {400, 404, 422}
                    ):
                        # Proxies sometimes advertise Responses but reject OpenAI-only
                        # cache/session parameters. The failed pre-body request keeps
                        # the same idempotency key, so stripping controls cannot create
                        # a second answer.
                        request_body.pop("previous_response_id", None)
                        request_body.pop("prompt_cache_key", None)
                        advanced_fallback_used = True
                        continue
                    if (
                        not emitted_text
                        and allow_degraded
                        and _can_degrade_stream(exc)
                        and (not exc.retryable or attempts >= self._max_retries)
                    ):
                        degraded = await self._generate_text_degraded(
                            client=client,
                            request_body=request_body,
                            idempotency_key=idempotency_key,
                        )
                        yield {"type": "degraded", "reason_code": "STREAMING_UNSUPPORTED"}
                        yield {"type": "delta", "delta": degraded["text"]}
                        yield {
                            "type": "completed",
                            "model": degraded["model"],
                            "input_tokens": degraded["input_tokens"],
                            "cached_input_tokens": degraded["cached_input_tokens"],
                            "cache_write_tokens": degraded["cache_write_tokens"],
                            "output_tokens": degraded["output_tokens"],
                            "reasoning_tokens": degraded["reasoning_tokens"],
                            "cache_policy": degraded["cache_policy"],
                            "response_id": degraded["response_id"],
                        }
                        return
                    if emitted_text or not exc.retryable or attempts >= self._max_retries:
                        raise
                attempts += 1
                if self._retry_backoff:
                    await asyncio.sleep(self._retry_backoff * (2 ** (attempts - 1)))
        finally:
            if owns_client:
                await client.aclose()

    async def probe_stream(self) -> bool:
        """Check native SSE support without silently using the chat fallback."""
        completed = False
        async for event in self.stream_text(
            messages=({"role": "user", "content": "Reply with ok."},),
            idempotency_key=f"stream-probe-{self._model}",
            allow_degraded=False,
        ):
            completed = completed or event.get("type") == "completed"
        return completed

    async def _generate_text_degraded(
        self,
        *,
        client: httpx.AsyncClient,
        request_body: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = dict(request_body)
        body["stream"] = False
        response = await client.post(
            f"{self._base_url}/responses",
            json=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Idempotency-Key": idempotency_key,
                "Accept": "application/json",
            },
            timeout=self._timeout,
        )
        if response.is_error:
            raise OpenAICompatibleError(
                _http_error_message(response),
                code=_status_error_code(response.status_code),
                status_code=response.status_code,
                retryable=response.status_code in _RETRYABLE_STATUS_CODES,
            )
        payload = _decode_response(response)
        text = _extract_output_text(payload).strip()
        if not text:
            raise OpenAICompatibleError("Responses API response contains no output text")
        usage = _usage(payload)
        return {
            "text": text,
            "model": str(payload.get("model") or self._model),
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cache_policy": self._cache_policy,
            "response_id": str(payload.get("id")) if isinstance(payload.get("id"), str) else None,
        }


class _RetryableResponseError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response


def _messages_to_input(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    input_messages: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role:
            raise ValueError(f"messages[{index}].role must be a non-empty string")
        if isinstance(content, str):
            part_type = "output_text" if role == "assistant" else "input_text"
            parts: list[dict[str, Any]] = [{"type": part_type, "text": content}]
        elif isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
            parts = []
            for part_index, raw_part in enumerate(content):
                if not isinstance(raw_part, Mapping):
                    raise ValueError(f"messages[{index}].content[{part_index}] must be an object")
                part = dict(raw_part)
                raw_part_type = part.get("type")
                allowed = {"output_text"} if role == "assistant" else {"input_text", "input_image"}
                if raw_part_type not in allowed:
                    raise ValueError(f"messages[{index}].content[{part_index}] has invalid type")
                if raw_part_type in {"input_text", "output_text"} and not isinstance(
                    part.get("text"), str
                ):
                    raise ValueError(
                        f"messages[{index}].content[{part_index}].text must be a string"
                    )
                if raw_part_type == "input_image" and not (
                    isinstance(part.get("image_url"), str) or isinstance(part.get("file_id"), str)
                ):
                    raise ValueError(
                        f"messages[{index}].content[{part_index}] requires image_url or file_id"
                    )
                parts.append(part)
        else:
            raise ValueError(f"messages[{index}].content must be text or a list of parts")
        input_messages.append({"role": role, "content": parts})
    return input_messages


def _status_error_code(status_code: int) -> str:
    if status_code == 429:
        return "MODEL_RATE_LIMITED"
    if status_code == 408:
        return "MODEL_TIMEOUT"
    if status_code >= 500:
        return "MODEL_GATEWAY_UNAVAILABLE"
    return "MODEL_RESPONSE_ERROR"


def _can_degrade_stream(error: OpenAICompatibleError) -> bool:
    """Only use a one-shot reply for gateways that reject/break SSE itself."""
    return error.code == "MODEL_STREAM_INCOMPLETE" or error.status_code in {
        400,
        404,
        405,
        406,
        415,
        501,
    }


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


def _usage(response: Mapping[str, Any]) -> ModelUsage:
    usage = response.get("usage", response)
    if not isinstance(usage, Mapping):
        return ModelUsage((0, 0, 0, 0, 0))
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    input_details = usage.get("input_tokens_details", usage.get("prompt_tokens_details", {}))
    output_details = usage.get("output_tokens_details", usage.get("completion_tokens_details", {}))
    input_details = input_details if isinstance(input_details, Mapping) else {}
    output_details = output_details if isinstance(output_details, Mapping) else {}
    cached_input = _first_usage_value(
        usage,
        input_details,
        keys=("cached_tokens", "cache_read_tokens", "cache_read_input_tokens"),
    )
    cache_write = _first_usage_value(
        usage,
        input_details,
        keys=("cache_write_tokens", "cache_creation_input_tokens", "cache_creation_tokens"),
    )
    reasoning = _first_usage_value(
        usage,
        output_details,
        keys=("reasoning_tokens",),
    )
    return ModelUsage(
        (
            _non_negative_int(input_tokens),
            cached_input,
            cache_write,
            _non_negative_int(output_tokens),
            reasoning,
        )
    )


def _first_usage_value(
    usage: Mapping[str, Any], details: Mapping[str, Any], *, keys: tuple[str, ...]
) -> int:
    for source in (details, usage):
        for key in keys:
            value = _non_negative_int(source.get(key, 0))
            if value:
                return value
    return 0


def _prompt_cache_key(supplied_key: str | None, messages: Sequence[Mapping[str, Any]]) -> str:
    if supplied_key:
        return supplied_key[:64]
    canonical = json.dumps(
        _messages_to_input(messages), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _http_error_message(response: httpx.Response) -> str:
    # Upstream bodies are untrusted and may reflect prompts, credentials, or
    # internal diagnostics. Persist and expose only the stable status code.
    return f"Responses API request failed with status {response.status_code}"
