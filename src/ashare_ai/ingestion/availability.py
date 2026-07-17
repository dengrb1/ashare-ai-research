from __future__ import annotations

from datetime import date, datetime

from pydantic import AwareDatetime

from ashare_ai.core.contracts import AvailabilityBasis, FrozenModel, PointInTimeRecord
from ashare_ai.core.time import conservative_date_availability, ensure_aware


class AvailabilityDecision(FrozenModel):
    available_at: AwareDatetime
    basis: AvailabilityBasis


def resolve_available_at(
    *,
    fetched_at: datetime,
    official_published_at: datetime | None = None,
    vendor_published_at: datetime | None = None,
    source_date: date | None = None,
) -> AvailabilityDecision:
    fetched_at = ensure_aware(fetched_at)
    if official_published_at is not None:
        return AvailabilityDecision(
            available_at=ensure_aware(official_published_at),
            basis=AvailabilityBasis.OFFICIAL_TIMESTAMP,
        )
    if vendor_published_at is not None:
        return AvailabilityDecision(
            available_at=ensure_aware(vendor_published_at),
            basis=AvailabilityBasis.VENDOR_TIMESTAMP,
        )
    if source_date is not None:
        return AvailabilityDecision(
            available_at=conservative_date_availability(source_date),
            basis=AvailabilityBasis.DATE_ONLY_CONSERVATIVE,
        )
    return AvailabilityDecision(
        available_at=fetched_at,
        basis=AvailabilityBasis.FIRST_OBSERVED,
    )


def derived_available_at(records: tuple[PointInTimeRecord, ...]) -> AvailabilityDecision:
    if not records:
        raise ValueError("derived availability requires at least one input record")
    return AvailabilityDecision(
        available_at=max(record.available_at for record in records),
        basis=AvailabilityBasis.DERIVED,
    )
