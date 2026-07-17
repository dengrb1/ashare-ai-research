from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.storage.models import ObjectManifestRow, ObjectOccurrenceRow
from ashare_ai.storage.objects import ObjectStore


class StoredObjectService:
    """Content-addressed object writes with an authoritative PostgreSQL manifest."""

    def __init__(self, session: Session, store: ObjectStore) -> None:
        self.session = session
        self.store = store

    def put(
        self,
        payload: bytes,
        *,
        content_type: str,
        source: str,
        source_record_id: str,
        fetched_at: datetime,
        available_at: datetime | None = None,
        details: dict[str, Any] | None = None,
    ) -> ObjectManifestRow:
        uri, digest = self.store.put(payload, content_type=content_type)
        existing = self.session.scalar(
            select(ObjectManifestRow).where(ObjectManifestRow.content_sha256 == digest)
        )
        if existing is None:
            existing = ObjectManifestRow(
                object_uri=uri,
                content_sha256=digest,
                mime_type=content_type,
                byte_size=len(payload),
            )
            self.session.add(existing)
            self.session.flush()

        occurrence = self.session.scalar(
            select(ObjectOccurrenceRow).where(
                ObjectOccurrenceRow.object_id == existing.object_id,
                ObjectOccurrenceRow.source == source,
                ObjectOccurrenceRow.source_record_id == source_record_id,
                ObjectOccurrenceRow.fetched_at == fetched_at,
            )
        )
        if occurrence is None:
            self.session.add(
                ObjectOccurrenceRow(
                    object_id=existing.object_id,
                    source=source,
                    source_record_id=source_record_id,
                    fetched_at=fetched_at,
                    available_at=available_at,
                    details=details or {},
                )
            )
        self.session.flush()
        return existing
