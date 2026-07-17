from __future__ import annotations

from typing import Any

import httpx

from ashare_ai.adapters._vendor import make_envelope
from ashare_ai.adapters.protocols import AdapterCapabilities, FetchRequest
from ashare_ai.core.contracts import RawEnvelope


class EastmoneyAdapter:
    source = "eastmoney"
    adapter_version = "1.0.0"
    capabilities = AdapterCapabilities(
        datasets=("securities", "daily_bars", "financials", "announcements", "news"),
        supports_incremental=True,
        supports_point_in_time=False,
    )

    def __init__(
        self,
        *,
        base_url: str = "https://push2.eastmoney.com",
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    async def fetch_raw(self, request: FetchRequest) -> tuple[RawEnvelope, ...]:
        path = request.operation if request.operation.startswith("/") else f"/{request.operation}"
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.get(
                f"{self._base_url}{path}", params=_query_parameters(request.parameters)
            )
            response.raise_for_status()
            return (make_envelope(self.source, request, response.content),)
        finally:
            if owns_client:
                await client.aclose()


def _query_parameters(parameters: dict[str, Any]) -> dict[str, str | int | float | bool]:
    allowed = (str, int, float, bool)
    normalized: dict[str, str | int | float | bool] = {}
    for key, value in parameters.items():
        if value is None:
            continue
        if not isinstance(value, allowed):
            raise TypeError(f"Eastmoney query parameter {key!r} must be scalar")
        normalized[key] = value
    return normalized
