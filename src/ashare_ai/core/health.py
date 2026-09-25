"""Infrastructure health check for the model gateway and quote bridge."""

from __future__ import annotations

import logging
from typing import Any

from ashare_ai.agents.gateway_client import get_gateway_client
from ashare_ai.core.config import Settings
from ashare_ai.market.quote_bridge_client import get_quote_bridge_client

logger = logging.getLogger(__name__)


def check_infrastructure_health(settings: Settings) -> dict[str, dict[str, Any]]:
    """Check health status of Gateway and Quote Bridge.

    Returns:
        Dictionary with service status, each containing:
        - status: "ok" | "unavailable"
        - url: service URL (optional)
        - error: error message if unavailable (optional)
    """
    result: dict[str, dict[str, Any]] = {}

    # Check Quote Bridge using the client
    if settings.quote_bridge_enabled:
        try:
            client = get_quote_bridge_client(settings)
            is_healthy = client.health()
            result["quote_bridge"] = {
                "status": "ok" if is_healthy else "unavailable",
                "url": settings.quote_bridge_url,
            }
            client.close()
        except Exception as exc:
            result["quote_bridge"] = {
                "status": "unavailable",
                "url": settings.quote_bridge_url,
                "error": str(exc),
            }

    # Check Gateway using the client
    if settings.gateway_enabled:
        try:
            client = get_gateway_client(settings)
            is_healthy = client.health()
            result["gateway"] = {
                "status": "ok" if is_healthy else "unavailable",
                "url": settings.gateway_url,
            }
            client.close()
        except Exception as exc:
            result["gateway"] = {
                "status": "unavailable",
                "url": settings.gateway_url,
                "error": str(exc),
            }

    return result
