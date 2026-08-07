from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ashare_ai.core.contracts import SnapshotManifest, SnapshotStatus
from ashare_ai.core.hashing import stable_hash


class ImmutableLake:
    """Writes content-addressed Parquet snapshots and queries only explicit manifests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_snapshot(
        self,
        *,
        dataset: str,
        source: str,
        schema_version: str,
        adapter_version: str,
        fetched_at: datetime,
        rows: Iterable[Mapping[str, Any]],
        metadata: Mapping[str, Any] | None = None,
    ) -> SnapshotManifest:
        materialized = list(rows)
        payload_hash = stable_hash(materialized)
        partition = (
            self.root
            / dataset
            / source
            / f"year={fetched_at.year:04d}"
            / f"month={fetched_at.month:02d}"
            / f"schema={stable_hash(schema_version)[:16]}"
            / f"adapter={stable_hash(adapter_version)[:16]}"
        )
        partition.mkdir(parents=True, exist_ok=True)
        temp = partition / f".{uuid4().hex}.parquet.tmp"
        table = pa.Table.from_pylist(materialized)
        table_metadata = {
            b"dataset": dataset.encode(),
            b"source": source.encode(),
            b"schema_version": schema_version.encode(),
            b"adapter_version": adapter_version.encode(),
            b"payload_sha256": payload_hash.encode(),
        }
        table = table.replace_schema_metadata(table_metadata)
        pq.write_table(table, temp, compression="zstd")
        file_hash = _file_sha256(temp)
        target = partition / f"{file_hash}.parquet"
        try:
            try:
                os.link(temp, target)
            except FileExistsError:
                _assert_file_hash(target, file_hash)
        finally:
            temp.unlink(missing_ok=True)
        return SnapshotManifest(
            dataset=dataset,
            source=source,
            schema_version=schema_version,
            adapter_version=adapter_version,
            fetched_at=fetched_at,
            row_count=len(materialized),
            payload_sha256=payload_hash,
            parquet_uri=target.resolve().as_uri(),
            status=SnapshotStatus.STAGING,
            metadata={**(metadata or {}), "parquet_file_sha256": file_hash},
        )

    def read_committed(self, manifests: Iterable[SnapshotManifest]) -> pa.Table:
        selected = list(manifests)
        if not selected:
            return pa.table({})
        paths = _validated_paths(selected)
        tables = [pq.ParquetFile(path).read() for path in paths]
        return pa.concat_tables(tables, promote_options="default")

    def query(self, sql: str, manifests: Iterable[SnapshotManifest]) -> list[dict[str, Any]]:
        """Execute a manifest-scoped query and materialize the result for compatibility."""
        rows: list[dict[str, Any]] = []
        for batch in self.query_batches(sql, manifests):
            rows.extend(batch.to_pylist())
        return rows

    def query_arrow(self, sql: str, manifests: Iterable[SnapshotManifest]) -> pa.Table:
        """Execute a manifest-scoped query and return an Arrow table.

        Callers that process large results should prefer :meth:`query_batches` to avoid
        materializing the complete result in memory.
        """
        batches = list(self.query_batches(sql, manifests))
        if not batches:
            return pa.table({})
        return pa.Table.from_batches(batches)

    def query_batches(
        self,
        sql: str,
        manifests: Iterable[SnapshotManifest],
        *,
        batch_size: int = 65_536,
    ) -> Iterator[pa.RecordBatch]:
        """Yield query results as Arrow record batches.

        Manifest validation happens before DuckDB opens the files, so this path has the
        same immutability and hash guarantees as ``read_committed`` and ``query``.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        selected = list(manifests)
        if not selected:
            raise ValueError("DuckDB queries require one or more committed manifests")
        paths = [str(path) for path in _validated_paths(selected)]
        with duckdb.connect(":memory:") as connection:
            connection.from_parquet(paths).create_view("snapshot")
            reader = connection.execute(sql).fetch_record_batch(rows_per_batch=batch_size)
            yield from reader


def _uri_to_path(uri: str) -> Path:
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path)
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return Path(path)
    return Path(uri)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_file_hash(path: Path, expected: str) -> None:
    if not path.is_file():
        raise ValueError(f"snapshot file does not exist: {path}")
    actual = _file_sha256(path)
    if actual != expected:
        raise ValueError(f"Parquet file hash mismatch: expected={expected}, actual={actual}")


def _validated_paths(manifests: list[SnapshotManifest]) -> list[Path]:
    invalid = [item.snapshot_id for item in manifests if item.status != SnapshotStatus.COMMITTED]
    if invalid:
        raise ValueError(f"refusing to read uncommitted snapshots: {invalid}")
    paths: list[Path] = []
    for manifest in manifests:
        expected = manifest.metadata.get("parquet_file_sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError(
                f"committed snapshot lacks parquet_file_sha256: {manifest.snapshot_id}"
            )
        path = _uri_to_path(manifest.parquet_uri)
        _assert_file_hash(path, expected)
        paths.append(path)
    return paths
