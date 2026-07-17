from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ashare_ai.adapters.protocols import DisclosureVerification, OfficialDisclosureVerifier
from ashare_ai.core.contracts import AvailabilityBasis, Disclosure


def apply_official_verification(
    disclosure: Disclosure, verification: DisclosureVerification
) -> Disclosure:
    if not verification.verified:
        return disclosure.model_copy(update={"official_verified": False, "official_source": None})
    if not verification.official_source:
        raise ValueError("verified disclosure requires official_source")
    update: dict[str, Any] = {
        "official_verified": True,
        "official_source": verification.official_source,
    }
    if verification.official_published_at is not None:
        update.update(
            available_at=verification.official_published_at,
            published_at=verification.official_published_at,
            availability_basis=AvailabilityBasis.OFFICIAL_TIMESTAMP,
        )
    return Disclosure.model_validate(disclosure.model_copy(update=update).model_dump())


async def verify_disclosure(
    disclosure: Disclosure,
    verifier: OfficialDisclosureVerifier,
    *,
    context: Mapping[str, Any] | None = None,
) -> Disclosure:
    verification = await verifier.verify(disclosure, context=context)
    return apply_official_verification(disclosure, verification)
