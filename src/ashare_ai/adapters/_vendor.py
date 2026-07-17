from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

from ashare_ai.adapters.protocols import FetchRequest
from ashare_ai.core.contracts import RawEnvelope
from ashare_ai.core.hashing import sha256_bytes, stable_hash


def serialize_vendor_result(result: Any) -> bytes:
    if hasattr(result, "to_json"):
        serialized = cast(
            str,
            result.to_json(orient="records", force_ascii=False, date_format="iso"),
        )
        return serialized.encode("utf-8")
    return json.dumps(
        result,
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
