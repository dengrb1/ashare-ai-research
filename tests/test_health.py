"""Tests for bridge/gateway health checks."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from ashare_ai.core.config import Settings
from ashare_ai.core.health import check_infrastructure_health


@pytest.fixture
def mock_settings() -> Settings:
    """Create test settings with all services enabled."""
    settings = Settings()
    settings.quote_bridge_enabled = True
    settings.quote_bridge_url = "http://localhost:8081"
    settings.news_bridge_enabled = True
    settings.news_bridge_url = "http://localhost:8082"
    settings.gateway_enabled = True
    settings.gateway_url = "http://localhost:8787"
    return settings


class TestCheckInfrastructureHealth:
    def test_all_services_healthy(self, mock_settings: Settings) -> None:
        with (
            patch("ashare_ai.core.health.get_quote_bridge_client") as mock_quote,
            patch("ashare_ai.core.health.get_news_bridge_client") as mock_news,
            patch("ashare_ai.core.health.get_gateway_client") as mock_gateway,
        ):
            # Mock all clients to return healthy status
            for mock_get in [mock_quote, mock_news, mock_gateway]:
                mock_client = Mock()
                mock_client.health.return_value = True
                mock_get.return_value = mock_client

            result = check_infrastructure_health(mock_settings)

            assert result["quote_bridge"]["status"] == "ok"
            assert result["quote_bridge"]["url"] == "http://localhost:8081"
            assert result["news_bridge"]["status"] == "ok"
            assert result["news_bridge"]["url"] == "http://localhost:8082"
            assert result["gateway"]["status"] == "ok"
            assert result["gateway"]["url"] == "http://localhost:8787"

    def test_service_unhealthy(self, mock_settings: Settings) -> None:
        with (
            patch("ashare_ai.core.health.get_quote_bridge_client") as mock_quote,
            patch("ashare_ai.core.health.get_news_bridge_client") as mock_news,
            patch("ashare_ai.core.health.get_gateway_client") as mock_gateway,
        ):
            # Quote Bridge healthy
            mock_quote_client = Mock()
            mock_quote_client.health.return_value = True
            mock_quote.return_value = mock_quote_client

            # News Bridge unhealthy
            mock_news_client = Mock()
            mock_news_client.health.return_value = False
            mock_news.return_value = mock_news_client

            # Gateway healthy
            mock_gateway_client = Mock()
            mock_gateway_client.health.return_value = True
            mock_gateway.return_value = mock_gateway_client

            result = check_infrastructure_health(mock_settings)

            assert result["quote_bridge"]["status"] == "ok"
            assert result["news_bridge"]["status"] == "unavailable"
            assert result["gateway"]["status"] == "ok"

    def test_service_exception(self, mock_settings: Settings) -> None:
        with (
            patch("ashare_ai.core.health.get_quote_bridge_client") as mock_quote,
            patch("ashare_ai.core.health.get_news_bridge_client") as mock_news,
            patch("ashare_ai.core.health.get_gateway_client") as mock_gateway,
        ):
            # Quote Bridge raises exception
            mock_quote.side_effect = ConnectionError("Connection refused")

            # News Bridge healthy
            mock_news_client = Mock()
            mock_news_client.health.return_value = True
            mock_news.return_value = mock_news_client

            # Gateway healthy
            mock_gateway_client = Mock()
            mock_gateway_client.health.return_value = True
            mock_gateway.return_value = mock_gateway_client

            result = check_infrastructure_health(mock_settings)

            assert result["quote_bridge"]["status"] == "unavailable"
            assert "error" in result["quote_bridge"]
            assert "Connection refused" in result["quote_bridge"]["error"]
            assert result["news_bridge"]["status"] == "ok"
            assert result["gateway"]["status"] == "ok"

    def test_disabled_services_not_checked(self, mock_settings: Settings) -> None:
        mock_settings.quote_bridge_enabled = False
        mock_settings.news_bridge_enabled = False
        mock_settings.gateway_enabled = False

        result = check_infrastructure_health(mock_settings)

        assert "quote_bridge" not in result
        assert "news_bridge" not in result
        assert "gateway" not in result
