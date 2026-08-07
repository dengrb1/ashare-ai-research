from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ashare_ai.core.contracts import SnapshotStatus
from ashare_ai.storage.lake import ImmutableLake, _uri_to_path
from ashare_ai.storage.models import (
    Base,
    ObjectManifestRow,
    ObjectOccurrenceRow,
    SnapshotManifestRow,
)
from ashare_ai.storage.object_service import StoredObjectService
from ashare_ai.storage.objects import LocalObjectStore
from ashare_ai.storage.repositories import SnapshotRepository

TZ = ZoneInfo("Asia/Shanghai")


def test_lake_reads_only_committed_manifests(tmp_path) -> None:
    lake = ImmutableLake(tmp_path / "lake")
    staging = lake.write_snapshot(
        dataset="daily_bar",
        source="fixture",
        schema_version="1",
        adapter_version="1",
        fetched_at=datetime(2026, 7, 14, 18, tzinfo=TZ),
        rows=[{"symbol": "600000.SH", "close": 10.0}],
    )
    with pytest.raises(ValueError, match="uncommitted"):
        lake.read_committed([staging])

    committed = staging.model_copy(update={"status": SnapshotStatus.COMMITTED})
    table = lake.read_committed([committed])
    assert table.to_pylist() == [{"symbol": "600000.SH", "close": 10.0}]
    result = lake.query("SELECT symbol, close FROM snapshot", [committed])
    assert result == [{"symbol": "600000.SH", "close": 10.0}]


def test_lake_streams_query_results_as_arrow_batches(tmp_path) -> None:
    lake = ImmutableLake(tmp_path / "lake")
    snapshot = lake.write_snapshot(
        dataset="daily_bar",
        source="fixture",
        schema_version="1",
        adapter_version="1",
        fetched_at=datetime(2026, 7, 14, 18, tzinfo=TZ),
        rows=[{"symbol": f"600{i:03d}.SH", "close": float(i)} for i in range(5)],
    ).model_copy(update={"status": SnapshotStatus.COMMITTED})

    batches = list(
        lake.query_batches(
            "SELECT symbol, close FROM snapshot ORDER BY symbol",
            [snapshot],
            batch_size=2,
        )
    )
    assert [batch.num_rows for batch in batches] == [2, 2, 1]
    assert [row for batch in batches for row in batch.to_pylist()] == [
        {"symbol": f"600{i:03d}.SH", "close": float(i)} for i in range(5)
    ]
    result = lake.query_arrow("SELECT close FROM snapshot ORDER BY close", [snapshot]).to_pylist()
    assert result == [
        {"close": float(i)} for i in range(5)
    ]
    empty = lake.query_arrow(
        "SELECT close FROM snapshot WHERE close > 100", [snapshot]
    ).to_pylist()
    assert empty == []


def test_lake_rejects_invalid_query_batch_size(tmp_path) -> None:
    lake = ImmutableLake(tmp_path / "lake")
    with pytest.raises(ValueError, match="batch_size must be positive"):
        list(lake.query_batches("SELECT 1", [], batch_size=0))


def test_snapshot_registration_is_idempotent_after_commit(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    manifest = ImmutableLake(tmp_path / "lake").write_snapshot(
        dataset="canonical_news",
        source="fixture",
        schema_version="1",
        adapter_version="1",
        fetched_at=datetime(2026, 7, 14, 18, tzinfo=TZ),
        rows=[{"run_id": "same-run", "headline": "immutable"}],
    )
    repository = SnapshotRepository(session)
    first = repository.add(manifest)
    repository.commit(first.snapshot_id)
    session.commit()

    second = repository.add(manifest)
    repository.commit(second.snapshot_id)
    session.commit()

    assert second.snapshot_id == first.snapshot_id
    assert session.query(SnapshotManifestRow).count() == 1


def test_lake_keeps_cross_version_snapshots_byte_immutable(tmp_path) -> None:
    lake = ImmutableLake(tmp_path / "lake")
    fetched_at = datetime(2026, 7, 14, 18, tzinfo=TZ)
    rows = [{"symbol": "600000.SH", "close": 10.0}]
    first = lake.write_snapshot(
        dataset="daily_bar",
        source="fixture",
        schema_version="1",
        adapter_version="1",
        fetched_at=fetched_at,
        rows=rows,
    )
    first_path = _uri_to_path(first.parquet_uri)
    first_bytes = first_path.read_bytes()

    second = lake.write_snapshot(
        dataset="daily_bar",
        source="fixture",
        schema_version="2",
        adapter_version="2",
        fetched_at=fetched_at,
        rows=rows,
    )
    assert first.parquet_uri != second.parquet_uri
    assert first_path.read_bytes() == first_bytes
    assert (
        lake.read_committed(
            [first.model_copy(update={"status": SnapshotStatus.COMMITTED})]
        ).to_pylist()
        == rows
    )


def test_lake_rejects_corrupted_committed_parquet(tmp_path) -> None:
    lake = ImmutableLake(tmp_path / "lake")
    snapshot = lake.write_snapshot(
        dataset="daily_bar",
        source="fixture",
        schema_version="1",
        adapter_version="1",
        fetched_at=datetime(2026, 7, 14, 18, tzinfo=TZ),
        rows=[{"symbol": "600000.SH", "close": 10.0}],
    ).model_copy(update={"status": SnapshotStatus.COMMITTED})
    path: Path = _uri_to_path(snapshot.parquet_uri)
    path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="Parquet file hash mismatch"):
        lake.read_committed([snapshot])
    with pytest.raises(ValueError, match="Parquet file hash mismatch"):
        lake.query("SELECT * FROM snapshot", [snapshot])


def test_content_addressed_object_store_detects_corruption(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    uri, digest = store.put(b"official disclosure", content_type="application/pdf")
    assert uri.endswith(digest)
    assert store.get(uri) == b"official disclosure"


def test_object_write_registers_and_deduplicates_manifest(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    service = StoredObjectService(session, LocalObjectStore(tmp_path / "objects"))
    fetched_at = datetime(2026, 7, 14, 18, tzinfo=TZ)
    first = service.put(
        b"announcement",
        content_type="application/pdf",
        source="exchange",
        source_record_id="notice-1",
        fetched_at=fetched_at,
        available_at=fetched_at,
    )
    second = service.put(
        b"announcement",
        content_type="application/pdf",
        source="exchange",
        source_record_id="notice-1",
        fetched_at=fetched_at,
        available_at=fetched_at,
    )
    third = service.put(
        b"announcement",
        content_type="application/pdf",
        source="eastmoney",
        source_record_id="notice-copy",
        fetched_at=fetched_at,
        available_at=fetched_at,
    )
    session.commit()
    assert first.object_id == second.object_id
    assert first.object_id == third.object_id
    assert session.query(ObjectManifestRow).count() == 1
    occurrences = session.query(ObjectOccurrenceRow).order_by(ObjectOccurrenceRow.source).all()
    assert [(item.source, item.source_record_id) for item in occurrences] == [
        ("eastmoney", "notice-copy"),
        ("exchange", "notice-1"),
    ]
