from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Any

from ashare_ai.adapters._vendor import make_envelope, serialize_vendor_result
from ashare_ai.adapters.protocols import AdapterCapabilities, FetchRequest
from ashare_ai.core.contracts import RawEnvelope


class AKShareAdapter:
    source = "akshare"
    adapter_version = "1.0.0"
    capabilities = AdapterCapabilities(
        datasets=("securities", "calendar", "daily_bars", "financials", "announcements"),
        supports_incremental=False,
        supports_point_in_time=False,
    )

    def _load_sdk(self) -> Any:
        try:
            return import_module("akshare")
        except ModuleNotFoundError as exc:
            message = "AKShare provider is optional; install the 'providers' extra"
            raise RuntimeError(message) from exc

    async def fetch_raw(self, request: FetchRequest) -> tuple[RawEnvelope, ...]:
        def invoke() -> Any:
            sdk = self._load_sdk()
            operation = getattr(sdk, request.operation, None)
            if operation is None or not callable(operation):
                raise ValueError(f"unknown AKShare operation: {request.operation}")
            return operation(**request.parameters)

        result = await asyncio.to_thread(invoke)
        payload = serialize_vendor_result(result)
        return (make_envelope(self.source, request, payload),)
