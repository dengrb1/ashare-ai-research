from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ashare_ai.core.contracts import (
    AvailabilityBasis,
    Board,
    Exchange,
    ManagerConclusion,
    SecurityMasterRecord,
)
from ashare_ai.core.point_in_time import latest_visible, visible_as_of
from ashare_ai.core.time import conservative_date_availability

TZ = ZoneInfo("Asia/Shanghai")
SHA = "a" * 64


def security(available_at: datetime, name: str) -> SecurityMasterRecord:
    return SecurityMasterRecord(
        symbol="600000.SH",
        trading_date=date(2026, 7, 14),
        available_at=available_at,
        source="exchange",
        source_record_id=name,
        fetched_at=available_at + timedelta(minutes=1),
        payload_sha256=SHA,
        adapter_version="test",
        ingestion_run_id=uuid4(),
        availability_basis=AvailabilityBasis.OFFICIAL_TIMESTAMP,
        exchange=Exchange.SH,
        board=Board.MAIN,
        short_name=name,
        list_date=date(1999, 11, 10),
        effective_from=date(2026, 7, 14),
    )


def test_future_records_do_not_change_cutoff_result() -> None:
    cutoff = datetime(2026, 7, 14, 18, tzinfo=TZ)
    known = security(cutoff - timedelta(minutes=1), "known")
    future = security(cutoff + timedelta(minutes=1), "future extreme")

    assert visible_as_of([known, future], cutoff) == [known]
    assert latest_visible([known, future], cutoff) == known


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        security(datetime(2026, 7, 14, 18), "naive")


def test_date_only_policy_is_conservative() -> None:
    value = conservative_date_availability(date(2026, 7, 14))
    assert value.hour == 23 and value.minute == 59
    assert value.tzinfo is not None


def test_manager_schema_rejects_final_score_fields() -> None:
    with pytest.raises(ValidationError):
        ManagerConclusion.model_validate({"summary": "ok", "total_score": 99})
