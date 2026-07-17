from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field

from ashare_ai.core.contracts import Disclosure, FrozenModel, RawEnvelope


class FetchRequest(FrozenModel):
    dataset: str
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    requested_at: AwareDatetime
    cursor: str | None = None


class AdapterCapabilities(FrozenModel):
    datasets: tuple[str, ...]
    supports_incremental: bool = False
    supports_point_in_time: bool = False
    rate_limit_per_minute: int | None = Field(default=None, gt=0)


class DisclosureVerification(FrozenModel):
    verified: bool
    official_source: str | None = None
    official_record_id: str | None = None
    official_published_at: AwareDatetime | None = None
    official_payload_sha256: str | None = None
    reason: str | None = None


@runtime_checkable
class RawDataAdapter(Protocol):
    source: str
    adapter_version: str
    capabilities: AdapterCapabilities

    async def fetch_raw(self, request: FetchRequest) -> tuple[RawEnvelope, ...]: ...


@runtime_checkable
class OfficialDisclosureVerifier(Protocol):
    async def verify(
        self, disclosure: Disclosure, *, context: Mapping[str, Any] | None = None
    ) -> DisclosureVerification: ...
