from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, Field

from ashare_ai.core.contracts import CanonicalSymbol, EvidenceRef, FrozenModel


class AgentRequest(FrozenModel):
    component: Literal["fundamental", "technical", "sentiment"]
    symbol: CanonicalSymbol
    decision_at: AwareDatetime
    prompt_version: str
    features: dict[str, float | int | str | bool | None]
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)


class GenerationMetadata(FrozenModel):
    provider: str
    model_name: str
    reasoning_effort: str
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cache_policy: Literal["GROK", "OPENAI", "COMPATIBLE"] = "COMPATIBLE"
    duration_ms: int = Field(ge=0)
    retry_count: int = Field(ge=0)


class StructuredGeneration(FrozenModel):
    output: dict[str, Any]
    metadata: GenerationMetadata


class StructuredLLMClient(Protocol):
    async def generate_structured(
        self,
        *,
        schema: type[BaseModel],
        messages: tuple[Mapping[str, str], ...],
        idempotency_key: str,
    ) -> StructuredGeneration: ...
