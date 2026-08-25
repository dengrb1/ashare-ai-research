"""Small, on-demand adapter for the public models.dev catalog."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx

MODELS_URL = "https://models.dev/models.json"
PROVIDERS_URL = "https://models.dev/api.json"


class ModelsDevError(RuntimeError):
    pass


async def lookup_models(query: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """Return selected catalog metadata without retaining a process-wide catalog."""

    normalized = query.strip().casefold()
    if len(normalized) < 2:
        raise ModelsDevError("请输入至少两个字符的模型名称")
    try:
        timeout = httpx.Timeout(12.0, connect=4.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            models_response, providers_response = await asyncio.gather(
                client.get(MODELS_URL), client.get(PROVIDERS_URL)
            )
        models_response.raise_for_status()
        providers_response.raise_for_status()
        models = models_response.json()
        providers = providers_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelsDevError("models.dev 元数据暂时不可用") from exc
    if not isinstance(models, dict) or not isinstance(providers, dict):
        raise ModelsDevError("models.dev 返回了无法识别的目录格式")

    selected: list[tuple[int, str, dict[str, Any]]] = []
    for canonical_id, raw in models.items():
        if not isinstance(canonical_id, str) or not isinstance(raw, dict):
            continue
        model_id = canonical_id.rsplit("/", 1)[-1]
        name = str(raw.get("name") or "")
        haystack = f"{canonical_id} {model_id} {name}".casefold()
        if normalized not in haystack:
            continue
        rank = 0 if normalized in {canonical_id.casefold(), model_id.casefold()} else 1
        selected.append((rank, canonical_id, raw))
    selected.sort(key=lambda item: (item[0], item[1]))
    return [
        _catalog_entry(canonical_id, raw, providers)
        for _, canonical_id, raw in selected[:limit]
    ]


def _catalog_entry(
    canonical_id: str, raw: dict[str, Any], providers: dict[str, Any]
) -> dict[str, Any]:
    lab, _, model_id = canonical_id.partition("/")
    raw_limit = raw.get("limit")
    limit: dict[Any, Any] = raw_limit if isinstance(raw_limit, dict) else {}
    context = _positive_int(limit.get("context"), default=128000)
    max_output = _positive_int(limit.get("output"), default=8192)
    source_model = _provider_model(providers, lab, model_id, canonical_id)
    raw_source_limit = source_model.get("limit")
    source_limit: dict[Any, Any] = (
        raw_source_limit if isinstance(raw_source_limit, dict) else {}
    )
    context = _positive_int(source_limit.get("context"), default=context)
    max_output = _positive_int(source_limit.get("output"), default=max_output)
    raw_cost = source_model.get("cost")
    cost: dict[Any, Any] = raw_cost if isinstance(raw_cost, dict) else {}
    policy = "OPENAI" if lab == "openai" else "GROK" if lab == "xai" else "COMPATIBLE"
    return {
        "canonical_id": canonical_id,
        # OpenAI-compatible gateways normally use the lab-free identifier.
        "model": model_id,
        "name": str(raw.get("name") or model_id),
        "context_window_tokens": context,
        # This editor setting is a reservation, not a claimed model maximum.
        "output_token_reserve": min(max_output, max(1024, context // 4)),
        "reasoning_token_reserve": min(8192, context // 8) if raw.get("reasoning") else 0,
        "input_price_per_million": _decimal(cost.get("input")),
        "cached_input_price_per_million": _decimal(cost.get("cache_read")),
        "cache_write_price_per_million": _decimal(cost.get("cache_write")),
        "output_price_per_million": _decimal(cost.get("output")),
        "cache_policy": policy,
        "reasoning": bool(raw.get("reasoning")),
        "structured_output": bool(raw.get("structured_output")),
        "source_url": f"https://models.dev/models/{canonical_id}",
    }


def _provider_model(
    providers: dict[str, Any], lab: str, model_id: str, canonical_id: str
) -> dict[str, Any]:
    provider = providers.get(lab)
    if isinstance(provider, dict) and isinstance(provider.get("models"), dict):
        for key in (model_id, canonical_id):
            candidate = provider["models"].get(key)
            if isinstance(candidate, dict):
                return candidate
    return {}


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return default
    try:
        parsed = int(value)
        return parsed if parsed >= 1024 else default
    except (TypeError, ValueError):
        return default


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
        return parsed if parsed >= 0 else Decimal("0")
    except Exception:
        return Decimal("0")
