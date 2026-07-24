from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.core.contracts import AgentComponentResult
from ashare_ai.storage.models import (
    AgentCall,
    AuditEvent,
    EvidenceRow,
    ObjectManifestRow,
    ObjectOccurrenceRow,
)


class AuditLogger:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        severity: str = "INFO",
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            run_id=run_id,
            event_type=event_type,
            severity=severity,
            message=message,
            details=details or {},
            created_at=datetime.now(UTC),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def record_agent_result(
        self,
        *,
        run_id: str,
        symbol: str,
        request_sha256: str,
        result: AgentComponentResult,
        created_at: datetime | None = None,
    ) -> AgentCall:
        call = AgentCall(
            run_id=run_id,
            symbol=symbol,
            component=result.component,
            model_provider=result.model_provider,
            model_name=result.model_name,
            reasoning_effort=result.reasoning_effort,
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            cache_write_tokens=result.cache_write_tokens,
            output_tokens=result.output_tokens,
            reasoning_tokens=result.reasoning_tokens,
            cache_policy=result.cache_policy,
            duration_ms=result.duration_ms,
            retry_count=result.retry_count,
            result_status="SUCCEEDED",
            request_sha256=request_sha256,
            response_sha256=result.response_sha256,
            result=result.model_dump(mode="json"),
            created_at=created_at or datetime.now(UTC),
        )
        self.session.add(call)
        for evidence in result.evidence:
            occurrence = self.session.scalar(
                select(ObjectOccurrenceRow)
                .join(
                    ObjectManifestRow,
                    ObjectManifestRow.object_id == ObjectOccurrenceRow.object_id,
                )
                .where(
                    ObjectOccurrenceRow.source == evidence.source,
                    ObjectOccurrenceRow.source_record_id == evidence.source_record_id,
                    ObjectManifestRow.content_sha256 == evidence.payload_sha256,
                )
                .order_by(ObjectOccurrenceRow.fetched_at.desc())
                .limit(1)
            )
            self.session.add(
                EvidenceRow(
                    evidence_id=evidence.evidence_id,
                    run_id=run_id,
                    symbol=symbol,
                    component=result.component,
                    evidence_type=evidence.evidence_type,
                    source=evidence.source,
                    source_record_id=evidence.source_record_id,
                    available_at=evidence.available_at,
                    payload_sha256=evidence.payload_sha256,
                    excerpt=evidence.excerpt,
                    object_occurrence_id=(
                        occurrence.occurrence_id if occurrence is not None else None
                    ),
                    object_uri=(
                        occurrence.object_manifest.object_uri if occurrence is not None else None
                    ),
                )
            )
        self.session.flush()
        return call
