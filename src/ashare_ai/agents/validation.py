from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from ashare_ai.agents.protocols import AgentRequest, StructuredGeneration, StructuredLLMClient
from ashare_ai.core.contracts import (
    AgentComponentResult,
    EvidenceRef,
    FrozenModel,
    ManagerConclusion,
)
from ashare_ai.core.hashing import canonical_json, sha256_bytes, stable_hash

_FORBIDDEN_MANAGER_KEYS = {
    "score",
    "total_score",
    "final_score",
    "rating",
    "weight",
    "target_price",
}
_FORBIDDEN_MANAGER_TEXT = re.compile(
    r"(?:最终|综合|总)(?:评分|得分)\s*[:：=]?\s*\d|target[_ ]?price\s*[:=]",
    flags=re.IGNORECASE,
)


class ComponentAnalysis(FrozenModel):
    """The model-owned portion of a component analysis.

    Transport metadata is deliberately absent: it is recorded from the client
    response and must never be supplied by a model.
    """

    component: str
    score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    evidence: tuple[EvidenceRef, ...]
    positive_factors: tuple[str, ...] = ()
    negative_factors: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()


def validate_component_payload(
    payload: Mapping[str, Any],
    *,
    request: AgentRequest,
    generation: StructuredGeneration | None = None,
) -> AgentComponentResult:
    data = dict(payload)
    prompt_sha256 = stable_hash(
        {
            "component": request.component,
            "symbol": request.symbol,
            "decision_at": request.decision_at,
            "prompt_version": request.prompt_version,
            "features": request.features,
            "evidence": request.evidence,
        }
    )
    if generation is not None:
        metadata = generation.metadata
        data.update(
            model_provider=metadata.provider,
            model_name=metadata.model_name,
            reasoning_effort=metadata.reasoning_effort,
            prompt_version=request.prompt_version,
            prompt_sha256=prompt_sha256,
            response_sha256=sha256_bytes(canonical_json(generation.output)),
            input_tokens=metadata.input_tokens,
            cached_input_tokens=metadata.cached_input_tokens,
            cache_write_tokens=metadata.cache_write_tokens,
            output_tokens=metadata.output_tokens,
            reasoning_tokens=metadata.reasoning_tokens,
            cache_policy=metadata.cache_policy,
            cache_layer=("SUPPLIER_PROMPT" if metadata.cached_input_tokens > 0 else "MISS"),
            duration_ms=metadata.duration_ms,
            retry_count=metadata.retry_count,
        )
    result = AgentComponentResult.model_validate(data)
    if result.component != request.component:
        raise ValueError("agent component does not match request")
    allowed_evidence = {
        (item.source, item.source_record_id, item.payload_sha256): item for item in request.evidence
    }
    for evidence in result.evidence:
        if evidence.available_at > request.decision_at:
            raise ValueError("agent evidence contains future information")
        identity = (evidence.source, evidence.source_record_id, evidence.payload_sha256)
        expected = allowed_evidence.get(identity)
        if expected is None:
            raise ValueError("agent returned evidence that was not present in its request")
        if evidence != expected:
            raise ValueError("agent evidence must exactly match evidence from its request")
    return result


def validate_manager_payload(payload: Mapping[str, Any]) -> ManagerConclusion:
    _reject_manager_decisions(payload)
    conclusion = ManagerConclusion.model_validate(payload)
    text = " ".join((conclusion.summary, *conclusion.thesis, *conclusion.risks))
    if _FORBIDDEN_MANAGER_TEXT.search(text):
        raise ValueError("manager may summarize but must not set a final score or target price")
    return conclusion


async def run_component_agent(
    client: StructuredLLMClient,
    request: AgentRequest,
    *,
    system_instruction: str,
    stable_prefix: str | None = None,
) -> AgentComponentResult:
    system_content = system_instruction
    if stable_prefix:
        system_content = f"{system_instruction}\n\n{stable_prefix}"
    messages = (
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": canonical_json(request).decode("utf-8"),
        },
    )
    idempotency_key = stable_hash({"request": request, "system": system_content})
    generation = await client.generate_structured(
        schema=ComponentAnalysis,
        messages=messages,
        idempotency_key=idempotency_key,
    )
    return validate_component_payload(generation.output, request=request, generation=generation)


async def run_manager_agent(
    client: StructuredLLMClient,
    component_results: Sequence[AgentComponentResult],
    *,
    system_instruction: str,
) -> ManagerConclusion:
    if len(component_results) != 3 or {result.component for result in component_results} != {
        "fundamental",
        "technical",
        "sentiment",
    }:
        raise ValueError("manager requires exactly the three component perspectives")
    messages = (
        {
            "role": "system",
            "content": (
                f"{system_instruction}\nOnly summarize evidence and disagreements. "
                "Do not calculate a score, rating, weight, or target price."
            ),
        },
        {
            "role": "user",
            "content": canonical_json(tuple(component_results)).decode("utf-8"),
        },
    )
    generation = await client.generate_structured(
        schema=ManagerConclusion,
        messages=messages,
        idempotency_key=stable_hash(
            {"components": tuple(component_results), "system": system_instruction}
        ),
    )
    return validate_manager_payload(generation.output)


def _reject_manager_decisions(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() in _FORBIDDEN_MANAGER_KEYS:
                raise ValueError(f"manager output contains forbidden decision field: {key}")
            _reject_manager_decisions(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _reject_manager_decisions(nested)
