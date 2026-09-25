"""Integration tests for Quote Bridge and Gateway clients."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from ashare_ai.agents.gateway_client import GatewayClient
from ashare_ai.core.config import Settings
from ashare_ai.market.quote_bridge_client import QuoteBridgeClient


@pytest.fixture
def test_settings() -> Settings:
    """Create test settings for integration tests."""
    settings = Settings()
    settings.quote_bridge_enabled = True
    settings.quote_bridge_url = "http://localhost:8081"
    settings.gateway_enabled = True
    settings.gateway_url = "http://localhost:8787"
    return settings


class TestQuoteBridgeIntegration:
    @respx.mock
    def test_get_quote_success(self, test_settings: Settings) -> None:
        respx.get("http://localhost:8081/quote").mock(
            return_value=Response(
                200,
                json={
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "price": 13.45,
                    "change": 0.12,
                    "change_percent": 0.90,
                    "open": 13.33,
                    "high": 13.50,
                    "low": 13.30,
                    "previous_close": 13.33,
                    "volume": 123456789,
                    "amount": 1234567890.0,
                    "timestamp": "2026-08-26T15:00:00+08:00",
                },
            )
        )

        client = QuoteBridgeClient(test_settings)
        quote = client.get_quote("000001.SZ")

        assert quote is not None
        assert quote["symbol"] == "000001.SZ"
        assert quote["price"] == 13.45
        assert quote["_trading_at"] == "2026-08-26T15:00:00+08:00"
        client.close()

    @respx.mock
    def test_get_quote_not_found(self, test_settings: Settings) -> None:
        respx.get("http://localhost:8081/quote").mock(return_value=Response(404))

        client = QuoteBridgeClient(test_settings)
        quote = client.get_quote("999999.SZ")

        assert quote is None
        client.close()

    @respx.mock
    def test_get_quotes_batch(self, test_settings: Settings) -> None:
        respx.get("http://localhost:8081/quote").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "price": 13.45,
                        "timestamp": "2026-08-26T15:00:00+08:00",
                    },
                    {
                        "symbol": "600000.SH",
                        "name": "浦发银行",
                        "price": 8.76,
                        "timestamp": "2026-08-26T15:00:00+08:00",
                    },
                ],
            )
        )

        client = QuoteBridgeClient(test_settings)
        quotes = client.get_quotes(["000001.SZ", "600000.SH"])

        assert len(quotes) == 2
        assert quotes[0]["symbol"] == "000001.SZ"
        assert quotes[1]["symbol"] == "600000.SH"
        client.close()


class TestGatewayIntegration:
    @respx.mock
    def test_chat_completion_success(self, test_settings: Settings) -> None:
        respx.post("http://localhost:8787/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "id": "chatcmpl-123",
                    "model": "gpt-4",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello!"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        client = GatewayClient(test_settings)
        response = client.chat_completion(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
        )

        assert response["id"] == "chatcmpl-123"
        assert response["choices"][0]["message"]["content"] == "Hello!"
        client.close()

    @respx.mock
    def test_embeddings_success(self, test_settings: Settings) -> None:
        respx.post("http://localhost:8787/v1/embeddings").mock(
            return_value=Response(
                200,
                json={
                    "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}],
                    "model": "text-embedding-3-small",
                    "usage": {"prompt_tokens": 5, "total_tokens": 5},
                },
            )
        )

        client = GatewayClient(test_settings)
        response = client.embeddings(
            model="text-embedding-3-small",
            input="Test text",
        )

        assert len(response["data"]) == 1
        assert len(response["data"][0]["embedding"]) == 3
        client.close()


class TestHealthChecks:
    @respx.mock
    def test_all_services_health(self, test_settings: Settings) -> None:
        respx.get("http://localhost:8081/health").mock(return_value=Response(200))
        respx.get("http://localhost:8787/health").mock(return_value=Response(200))

        quote_client = QuoteBridgeClient(test_settings)
        gateway_client = GatewayClient(test_settings)

        assert quote_client.health() is True
        assert gateway_client.health() is True

        quote_client.close()
        gateway_client.close()

    @respx.mock
    def test_service_unhealthy(self, test_settings: Settings) -> None:
        respx.get("http://localhost:8081/health").mock(return_value=Response(503))

        client = QuoteBridgeClient(test_settings)
        assert client.health() is False
        client.close()
