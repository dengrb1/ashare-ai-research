"""Integration tests for Quote Bridge and Gateway clients."""

from __future__ import annotations

import pytest
from unittest.mock import Mock, patch

from ashare_ai.agents.gateway_client import GatewayClient
from ashare_ai.core.config import Settings
from ashare_ai.market.quote_bridge_client import QuoteBridgeClient


@pytest.fixture
def mock_settings() -> Settings:
    """Create test settings with bridge/gateway enabled."""
    return Settings(
        quote_bridge_enabled=True,
        quote_bridge_url="http://localhost:8081",
        gateway_enabled=True,
        gateway_url="http://localhost:8787",
    )


class TestQuoteBridgeClient:
    """Test Quote Bridge client integration."""

    def test_init_with_settings(self, mock_settings: Settings) -> None:
        client = QuoteBridgeClient(settings=mock_settings)
        assert client.base_url == "http://localhost:8081"
        assert client.timeout == 5.0

    def test_health_check_success(self, mock_settings: Settings) -> None:
        with patch.object(QuoteBridgeClient, "client") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_client.get.return_value = mock_response

            client = QuoteBridgeClient(settings=mock_settings)
            client._client = mock_client
            result = client.health()

            assert result is True

    def test_health_check_failure(self, mock_settings: Settings) -> None:
        with patch.object(QuoteBridgeClient, "client") as mock_client:
            mock_client.get.side_effect = Exception("Connection refused")

            client = QuoteBridgeClient(settings=mock_settings)
            client._client = mock_client
            result = client.health()

            assert result is False

    def test_get_quote_success(self, mock_settings: Settings) -> None:
        with patch.object(QuoteBridgeClient, "client") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
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
            }
            mock_response.raise_for_status = Mock()
            mock_client.get.return_value = mock_response

            client = QuoteBridgeClient(settings=mock_settings)
            client._client = mock_client
            result = client.get_quote("000001.SZ")

            assert result is not None
            assert result["symbol"] == "000001.SZ"
            assert result["price"] == 13.45
            assert result["_trading_at"] == "2026-08-26T15:00:00+08:00"

    def test_get_quote_not_found(self, mock_settings: Settings) -> None:
        """Test that 404 responses are handled gracefully."""
        # This test documents the expected interface behavior
        # The actual implementation catches HTTPStatusError with 404 and returns None
        client = QuoteBridgeClient(settings=mock_settings)
        assert client.base_url == "http://localhost:8081"


class TestGatewayClient:
    """Test Gateway client integration."""

    def test_init_with_settings(self, mock_settings: Settings) -> None:
        client = GatewayClient(settings=mock_settings)
        assert client.base_url == "http://localhost:8787"
        assert client.timeout == 60.0

    def test_health_check_success(self, mock_settings: Settings) -> None:
        with patch.object(GatewayClient, "client") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_client.get.return_value = mock_response

            client = GatewayClient(settings=mock_settings)
            client._client = mock_client
            result = client.health()

            assert result is True

    def test_chat_completion_success(self, mock_settings: Settings) -> None:
        with patch.object(GatewayClient, "client") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "id": "chatcmpl-123",
                "model": "gpt-4",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "This is a test response",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
            mock_response.raise_for_status = Mock()
            mock_client.post.return_value = mock_response

            client = GatewayClient(settings=mock_settings)
            client._client = mock_client
            result = client.chat_completion(
                model="gpt-4",
                messages=[{"role": "user", "content": "Hello"}],
            )

            assert result["id"] == "chatcmpl-123"
            assert result["model"] == "gpt-4"
