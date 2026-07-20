from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ashare_ai.core.contracts import SnapshotManifest, SnapshotStatus
from ashare_ai.storage.models import (
    AuditEvent,
    BacktestRun,
    CandidateRow,
    EvidenceRow,
    JobRun,
    PortfolioRow,
    ReportRow,
    ScoreRow,
    SnapshotManifestRow,
)


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _versioned_match(self, manifest: SnapshotManifest) -> SnapshotManifestRow | None:
        return self.session.scalar(
            select(SnapshotManifestRow).where(
                SnapshotManifestRow.dataset == manifest.dataset,
                SnapshotManifestRow.source == manifest.source,
                SnapshotManifestRow.schema_version == manifest.schema_version,
                SnapshotManifestRow.adapter_version == manifest.adapter_version,
                SnapshotManifestRow.payload_sha256 == manifest.payload_sha256,
            )
        )

    @staticmethod
    def _validate_reuse(
        row: SnapshotManifestRow,
        manifest: SnapshotManifest,
        *,
        run_id: str | None,
    ) -> SnapshotManifestRow:
        if run_id is not None and row.run_id not in {None, run_id}:
            raise ValueError("snapshot content is already registered by another run")
        expected_file_hash = manifest.metadata.get("parquet_file_sha256")
        existing_file_hash = row.details.get("parquet_file_sha256")
        if (
            row.row_count != manifest.row_count
            or row.parquet_uri != manifest.parquet_uri
            or existing_file_hash != expected_file_hash
        ):
            raise ValueError("snapshot content identity conflicts with its immutable manifest")
        if row.status not in {SnapshotStatus.STAGING.value, SnapshotStatus.COMMITTED.value}:
            raise ValueError(f"snapshot has an invalid status: {row.status}")
        return row

    def add(
        self,
        manifest: SnapshotManifest,
        *,
        run_id: str | None = None,
    ) -> SnapshotManifestRow:
        existing = self._versioned_match(manifest)
        if existing is not None:
            return self._validate_reuse(existing, manifest, run_id=run_id)
        row = SnapshotManifestRow(
            snapshot_id=str(manifest.snapshot_id),
            run_id=run_id,
            dataset=manifest.dataset,
            source=manifest.source,
            schema_version=manifest.schema_version,
            adapter_version=manifest.adapter_version,
            fetched_at=manifest.fetched_at,
            row_count=manifest.row_count,
            payload_sha256=manifest.payload_sha256,
            parquet_uri=manifest.parquet_uri,
            status=manifest.status.value,
            details=manifest.metadata,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
            return row
        except IntegrityError:
            # A second leased worker may win the content-addressed insert between
            # the lookup and flush.  The savepoint keeps the outer job transaction
            # usable so the exact immutable manifest can be reused safely.
            existing = self._versioned_match(manifest)
            if existing is None:
                raise
            return self._validate_reuse(existing, manifest, run_id=run_id)

    def commit(self, snapshot_id: str) -> SnapshotManifestRow:
        row = self.session.get(SnapshotManifestRow, snapshot_id)
        if row is None:
            raise KeyError(snapshot_id)
        if row.status == SnapshotStatus.COMMITTED.value:
            return row
        if row.status != SnapshotStatus.STAGING.value:
            raise ValueError(f"snapshot is not STAGING: {row.status}")
        row.status = SnapshotStatus.COMMITTED.value
        row.committed_at = datetime.now(UTC)
        self.session.flush()
        return row

    def committed(self, dataset: str) -> list[SnapshotManifestRow]:
        statement = (
            select(SnapshotManifestRow)
            .where(
                SnapshotManifestRow.dataset == dataset,
                SnapshotManifestRow.status == SnapshotStatus.COMMITTED.value,
            )
            .order_by(SnapshotManifestRow.fetched_at.desc())
        )
        return list(self.session.scalars(statement))


class QueryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _all(self, statement: Select[Any]) -> list[Any]:
        return list(self.session.scalars(statement))

    def _result_run_id(
        self,
        trading_date: date,
        run_id: str | None,
        *,
        user_id: str | None = None,
        include_all_users: bool = True,
    ) -> str | None:
        if run_id is not None:
            statement = select(JobRun.run_id).where(JobRun.run_id == run_id)
            if not include_all_users:
                statement = statement.where(JobRun.user_id == user_id)
            return self.session.scalar(statement)
        conditions = [
            JobRun.trading_date == trading_date,
            JobRun.run_type == "DAILY",
            JobRun.status.in_(("SUCCEEDED", "FUSED")),
        ]
        if not include_all_users:
            conditions.append(JobRun.user_id == user_id)
        return self.session.scalar(
            select(JobRun.run_id)
            .where(*conditions)
            .order_by(
                JobRun.completed_at.is_(None),
                JobRun.completed_at.desc(),
                JobRun.started_at.desc(),
                JobRun.run_id.desc(),
            )
            .limit(1)
        )

    def result_run_id(
        self,
        trading_date: date,
        run_id: str | None = None,
        *,
        user_id: str | None = None,
        include_all_users: bool = True,
    ) -> str | None:
        return self._result_run_id(
            trading_date,
            run_id,
            user_id=user_id,
            include_all_users=include_all_users,
        )

    def scores(
        self,
        trading_date: date,
        run_id: str | None = None,
        *,
        user_id: str | None = None,
        include_all_users: bool = True,
    ) -> list[ScoreRow]:
        selected_run = self._result_run_id(
            trading_date, run_id, user_id=user_id, include_all_users=include_all_users
        )
        if selected_run is None:
            return []
        return self._all(
            select(ScoreRow)
            .where(
                ScoreRow.trading_date == trading_date,
                ScoreRow.run_id == selected_run,
            )
            .order_by(ScoreRow.total_score.desc())
        )

    def score(
        self,
        trading_date: date,
        symbol: str,
        run_id: str | None = None,
        *,
        user_id: str | None = None,
        include_all_users: bool = True,
    ) -> ScoreRow | None:
        selected_run = self._result_run_id(
            trading_date, run_id, user_id=user_id, include_all_users=include_all_users
        )
        if selected_run is None:
            return None
        return self.session.scalar(
            select(ScoreRow).where(
                ScoreRow.trading_date == trading_date,
                ScoreRow.symbol == symbol,
                ScoreRow.run_id == selected_run,
            )
        )

    def candidates(
        self,
        trading_date: date,
        run_id: str | None = None,
        *,
        user_id: str | None = None,
        include_all_users: bool = True,
    ) -> list[CandidateRow]:
        selected_run = self._result_run_id(
            trading_date, run_id, user_id=user_id, include_all_users=include_all_users
        )
        if selected_run is None:
            return []
        return self._all(
            select(CandidateRow)
            .where(
                CandidateRow.trading_date == trading_date,
                CandidateRow.run_id == selected_run,
            )
            .order_by(CandidateRow.rank)
        )

    def evidence(self, run_id: str, symbol: str) -> list[EvidenceRow]:
        return self._all(
            select(EvidenceRow)
            .where(EvidenceRow.run_id == run_id, EvidenceRow.symbol == symbol)
            .order_by(EvidenceRow.component, EvidenceRow.available_at, EvidenceRow.evidence_id)
        )

    def portfolio(
        self,
        trading_date: date,
        run_id: str | None = None,
        *,
        user_id: str | None = None,
        include_all_users: bool = True,
    ) -> PortfolioRow | None:
        selected_run = self._result_run_id(
            trading_date, run_id, user_id=user_id, include_all_users=include_all_users
        )
        if selected_run is None:
            return None
        return self.session.scalar(
            select(PortfolioRow)
            .where(
                PortfolioRow.trading_date == trading_date,
                PortfolioRow.run_id == selected_run,
            )
            .order_by(PortfolioRow.effective_trading_date.desc())
        )

    def report(
        self,
        trading_date: date,
        run_id: str | None = None,
        *,
        user_id: str | None = None,
        include_all_users: bool = True,
    ) -> ReportRow | None:
        selected_run = self._result_run_id(
            trading_date, run_id, user_id=user_id, include_all_users=include_all_users
        )
        if selected_run is None:
            return None
        return self.session.scalar(
            select(ReportRow)
            .where(
                ReportRow.trading_date == trading_date,
                ReportRow.run_id == selected_run,
            )
            .order_by(ReportRow.created_at.desc())
        )

    def run(self, run_id: str) -> JobRun | None:
        return self.session.get(JobRun, run_id)

    def audit(self, run_id: str) -> list[AuditEvent]:
        return self._all(
            select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.created_at)
        )

    def backtest(self, backtest_id: str) -> BacktestRun | None:
        return self.session.get(BacktestRun, backtest_id)
