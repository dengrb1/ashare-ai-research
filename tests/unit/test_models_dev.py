from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from ashare_ai.agents.models_dev import MODELS_URL, PROVIDERS_URL, lookup_models


@pytest.mark.asyncio
@respx.mock
async def test_models_dev_maps_context_reserves_cache_and_reference_prices() -> None:
    respx.get(MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "openai/gpt-test": {
                    "id": "openai/gpt-test",
                    "name": "GPT Test",
                    "reasoning": True,
                    "structured_output": True,
                    "limit": {"context": 200000, "output": 32000},
                }
            },
        )
    )
    respx.get(PROVIDERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "openai": {
                    "models": {
                        "gpt-test": {
                            "limit": {"context": 200000, "output": 32000},
                            "cost": {
                                "input": 1.25,
                                "output": 5,
                                "cache_read": 0.25,
                                "cache_write": 1.5,
                            },
                        }
                    }
                }
            },
        )
    )

    models = await lookup_models("gpt-test")

    assert len(models) == 1
    assert models[0]["model"] == "gpt-test"
    assert models[0]["context_window_tokens"] == 200000
    assert models[0]["output_token_reserve"] == 32000
    assert models[0]["reasoning_token_reserve"] == 8192
    assert models[0]["cache_policy"] == "OPENAI"
    assert models[0]["input_price_per_million"] == Decimal("1.25")
    assert models[0]["cached_input_price_per_million"] == Decimal("0.25")
