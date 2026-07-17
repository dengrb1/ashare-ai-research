from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from ashare_ai.core.contracts import FrozenModel, RawEnvelope, Sha256


class SourceAuditRecord(FrozenModel):
    audit_id: UUID = Field(default_factory=uuid4)
    ingestion_run_id: UUID
    source: str
    dataset: str
    source_record_id: str
    fetched_at: AwareDatetime
    payload_sha256: Sha256
    payload_size: int = Field(ge=0)
    adapter_version: str
    succeeded: bool = True
    error_code: str | None = None


def audit_envelope(
    envelope: RawEnvelope,
    *,
    ingestion_run_id: UUID,
    adapter_version: str,
    succeeded: bool = True,
    error_code: str | None = None,
) -> SourceAuditRecord:
    if succeeded and error_code is not None:
        raise ValueError("successful source audit cannot contain an error code")
    return SourceAuditRecord(
        ingestion_run_id=ingestion_run_id,
        source=envelope.source,
        dataset=envelope.dataset,
        source_record_id=envelope.source_record_id,
        fetched_at=envelope.fetched_at,
        payload_sha256=envelope.payload_sha256,
        payload_size=len(envelope.payload),
        adapter_version=adapter_version,
        succeeded=succeeded,
        error_code=error_code,
    )
