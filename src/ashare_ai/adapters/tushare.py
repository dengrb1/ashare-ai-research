from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Any

from ashare_ai.adapters._vendor import make_envelope, serialize_vendor_result
from ashare_ai.adapters.protocols import AdapterCapabilities, FetchRequest
from ashare_ai.core.contracts import RawEnvelope


class TushareAdapter:
    source = "tushare"
    adapter_version = "1.0.0"
    capabilities = AdapterCapabilities(
        datasets=("securities", "calendar", "daily_bars", "financials", "announcements"),
        supports_incremental=True,
        supports_point_in_time=True,
        rate_limit_per_minute=200,
    )

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("Tushare token is required")
        self._token = token

    def _load_sdk(self) -> Any:
        try:
            return import_module("tushare")
        except ModuleNotFoundError as exc:
            message = "Tushare provider is optional; install the 'providers' extra"
            raise RuntimeError(message) from exc

    async def fetch_raw(self, request: FetchRequest) -> tuple[RawEnvelope, ...]:
        def invoke() -> Any:
            client = self._load_sdk().pro_api(self._token)
            operation = getattr(client, request.operation, None)
            if operation is None or not callable(operation):
                raise ValueError(f"unknown Tushare operation: {request.operation}")
            return operation(**request.parameters)

        result = await asyncio.to_thread(invoke)
        payload = serialize_vendor_result(result)
        return (make_envelope(self.source, request, payload),)
