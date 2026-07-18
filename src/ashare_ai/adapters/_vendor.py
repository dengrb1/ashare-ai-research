from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from numbers import Real
from typing import Any

from ashare_ai.adapters.protocols import FetchRequest
from ashare_ai.core.contracts import RawEnvelope
from ashare_ai.core.hashing import sha256_bytes, stable_hash


def normalize_vendor_value(value: Any) -> Any:
    """Return vendor data with missing/non-finite scalars made JSON-safe."""
    if value is None or type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, Real):
        return value if math.isfinite(float(value)) else None
    if isinstance(value, Mapping):
        return {str(key): normalize_vendor_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize_vendor_value(item) for item in value]

    # NumPy scalars/arrays expose one of these without requiring the adapter
    # boundary to import either NumPy or pandas directly.
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            scalar = item_method()
        except (TypeError, ValueError):
            scalar = value
        if scalar is not value:
            return normalize_vendor_value(scalar)
    tolist_method = getattr(value, "tolist", None)
    if callable(tolist_method):
        return normalize_vendor_value(tolist_method())
    return value


def vendor_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    rows = frame.to_dict(orient="records")
    if not isinstance(rows, list):
        return []
    return [
        normalized
        for row in rows
        if isinstance(row, Mapping)
        and isinstance((normalized := normalize_vendor_value(row)), dict)
    ]


def serialize_vendor_result(result: Any) -> bytes:
    if hasattr(result, "to_dict"):
        result = vendor_records(result)
    return json.dumps(
        normalize_vendor_value(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode("utf-8")


def make_envelope(
    source: str,
    request: FetchRequest,
    payload: bytes,
    *,
    fetched_at: datetime | None = None,
) -> RawEnvelope:
    request_id = stable_hash(
        {
            "source": source,
            "dataset": request.dataset,
            "operation": request.operation,
            "parameters": request.parameters,
            "cursor": request.cursor,
        }
    )
    return RawEnvelope(
        source=source,
        dataset=request.dataset,
        source_record_id=request_id,
        fetched_at=fetched_at or datetime.now(UTC),
        content_type="application/json",
        payload=payload,
        payload_sha256=sha256_bytes(payload),
        cursor=request.cursor,
        request_metadata={
            "operation": request.operation,
            "parameters": request.parameters,
            "requested_at": request.requested_at.isoformat(),
        },
    )
