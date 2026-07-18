from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from ashare_ai.adapters._vendor import make_envelope, serialize_vendor_result, vendor_records
from ashare_ai.adapters.protocols import DisclosureVerification, FetchRequest
from ashare_ai.adapters.symbols import normalize_symbol
from ashare_ai.core.contracts import (
    AdjustmentFactor,
    AvailabilityBasis,
    DailyBar,
    Disclosure,
    RawEnvelope,
)
from ashare_ai.core.hashing import sha256_bytes
from ashare_ai.ingestion.adjustment import adjust_daily_bar, adjust_price
from ashare_ai.ingestion.availability import resolve_available_at
from ashare_ai.ingestion.disclosures import apply_official_verification

SHANGHAI = ZoneInfo("Asia/Shanghai")
HASH = "a" * 64


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600000", "600000.SH"),
        ("sh600000", "600000.SH"),
        ("600000.XSHG", "600000.SH"),
        ("SZ.000001", "000001.SZ"),
        ("300750.sz", "300750.SZ"),
        ("430047", "430047.BJ"),
        ("920001", "920001.BJ"),
    ],
)
def test_normalize_symbol(raw: str, expected: str) -> None:
    assert normalize_symbol(raw) == expected


def test_normalize_symbol_rejects_conflicting_exchange() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        normalize_symbol("600000.SH", exchange="SZ")


def test_raw_envelope_validates_payload_hash() -> None:
    payload = b'{"ok":true}'
    envelope = RawEnvelope(
        source="fixture",
        dataset="daily_bars",
        source_record_id="row-1",
        fetched_at=_at(2026, 7, 15, 18),
        content_type="application/json",
        payload=payload,
        payload_sha256=sha256_bytes(payload),
    )
    assert envelope.payload == payload
    with pytest.raises(ValidationError, match="payload_sha256"):
        RawEnvelope(
            source="fixture",
            dataset="daily_bars",
            source_record_id="row-1",
            fetched_at=_at(2026, 7, 15, 18),
            content_type="application/json",
            payload=payload,
            payload_sha256="0" * 64,
        )


def test_vendor_envelope_records_response_time_not_request_start() -> None:
    requested_at = datetime.now(UTC) - timedelta(minutes=5)
    request = FetchRequest(
        dataset="daily_bars",
        operation="fixture",
        requested_at=requested_at,
    )
    before = datetime.now(UTC)
    envelope = make_envelope("fixture", request, b"[]")
    after = datetime.now(UTC)
    assert before <= envelope.fetched_at <= after
    assert envelope.fetched_at != requested_at
    assert envelope.request_metadata["requested_at"] == requested_at.isoformat()


def test_vendor_records_and_payload_normalize_non_finite_values_recursively() -> None:
    frame = pd.DataFrame(
        [
            {
                "python_nan": float("nan"),
                "numpy_inf": np.float64("inf"),
                "pandas_missing": pd.NA,
                "finite": np.float64("12.5"),
                "nested": {"negative_inf": float("-inf"), "values": [1, np.nan]},
            }
        ]
    )

    records = vendor_records(frame)
    assert records == [
        {
            "python_nan": None,
            "numpy_inf": None,
            "pandas_missing": None,
            "finite": 12.5,
            "nested": {"negative_inf": None, "values": [1, None]},
        }
    ]
    payload = serialize_vendor_result(frame)
    assert serialize_vendor_result(frame) == payload
    assert b"NaN" not in payload and b"Infinity" not in payload
    decoded = json.loads(
        payload.decode("utf-8"), parse_constant=lambda value: pytest.fail(value)
    )
    assert decoded == records


def test_available_at_policy_is_conservative_and_prioritized() -> None:
    fetched = _at(2026, 7, 16, 9)
    official = _at(2026, 7, 15, 20)
    vendor = _at(2026, 7, 15, 19)
    resolved = resolve_available_at(
        fetched_at=fetched,
        official_published_at=official,
        vendor_published_at=vendor,
        source_date=date(2026, 7, 15),
    )
    assert resolved.available_at == official
    assert resolved.basis == AvailabilityBasis.OFFICIAL_TIMESTAMP

    date_only = resolve_available_at(fetched_at=fetched, source_date=date(2026, 7, 15))
    assert date_only.available_at.hour == 23
    assert date_only.available_at.minute == 59
    assert date_only.basis == AvailabilityBasis.DATE_ONLY_CONSERVATIVE

    first_seen = resolve_available_at(fetched_at=fetched)
    assert first_seen.available_at == fetched
    assert first_seen.basis == AvailabilityBasis.FIRST_OBSERVED


def test_official_disclosure_verification_can_move_availability_later() -> None:
    disclosure = Disclosure(
        **_pit("600000.SH", date(2026, 7, 15), _at(2026, 7, 15, 18)),
        announcement_id="ann-1",
        title="业绩预增公告",
        published_at=_at(2026, 7, 15, 18),
        document_uri="s3://bucket/ann-1.pdf",
        document_sha256=HASH,
    )
    verified = apply_official_verification(
        disclosure,
        DisclosureVerification(
            verified=True,
            official_source="SSE",
            official_record_id="sse-1",
            official_published_at=_at(2026, 7, 15, 20),
            official_payload_sha256=HASH,
        ),
    )
    assert verified.official_verified is True
    assert verified.official_source == "SSE"
    assert verified.available_at == _at(2026, 7, 15, 20)
    assert verified.availability_basis == AvailabilityBasis.OFFICIAL_TIMESTAMP


def test_adjustment_is_pure_and_uses_explicit_reference_factor() -> None:
    assert adjust_price(Decimal("10"), Decimal("1.2"), reference_factor=Decimal("1.5")) == 8
    bar = DailyBar(
        **_pit("600000.SH", date(2026, 7, 15), _at(2026, 7, 15, 18)),
        open=Decimal("9.8"),
        high=Decimal("10.2"),
        low=Decimal("9.7"),
        close=Decimal("10"),
        volume=Decimal("1000"),
        amount=Decimal("10000"),
    )
    factor = AdjustmentFactor(
        **_pit("600000.SH", date(2026, 7, 15), _at(2026, 7, 15, 18)),
        factor=Decimal("1.2"),
        factor_method="vendor",
    )
    adjusted = adjust_daily_bar(bar, factor, reference_factor=Decimal("1.5"))
    assert adjusted.close == Decimal("8")
    assert bar.close == Decimal("10")


def _at(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=SHANGHAI)


def _pit(symbol: str, trading_date: date, available_at: datetime) -> dict[str, object]:
    return {
        "symbol": symbol,
        "trading_date": trading_date,
        "available_at": available_at,
        "source": "fixture",
        "source_record_id": f"{symbol}-{trading_date}",
        "fetched_at": available_at,
        "payload_sha256": HASH,
        "adapter_version": "test",
        "ingestion_run_id": uuid4(),
        "availability_basis": AvailabilityBasis.VENDOR_TIMESTAMP,
    }
