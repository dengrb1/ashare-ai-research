"""Cascade deletion of runs and their derived research/log rows.

Deleting a run is a deliberate, administrator-requested cleanup: it removes
the run record together with its audit events, agent calls, evidence, scores,
candidates, portfolios, reports and trade plans.  Immutable data-lake objects
are preserved: snapshot and backtest rows keep their content and only drop the
run reference (their ``run_id`` is set to NULL rather than deleting them).
"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ashare_ai.storage.models import (
    AgentCall,
    AuditEvent,
    BacktestRun,
    BuyEntryMonitorRow,
    CandidateRow,
    EvidenceRow,
    ExitAdviceRow,
    JobRun,
    NotificationRow,
    PortfolioRow,
    ReportRow,
    ScoreRow,
    SnapshotManifestRow,
    TradePlanRow,
)

# Fail closed: only these run statuses may be removed.  Any active or unknown
# status refuses deletion so a running job can never be torn out from under a
# leased worker.
TERMINAL_RUN_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "FUSED", "UNAVAILABLE"}
)


def delete_run(session: Session, run: JobRun) -> None:
    """Permanently delete one terminal run and every derived index row.

    The caller must already hold the run row, verified ownership and a terminal
    status.  The immutable snapshot/backtest content is retained and only its
    run reference is detached.
    """

    run_id = run.run_id
    report_ids = list(
        session.scalars(select(ReportRow.report_id).where(ReportRow.run_id == run_id))
    )
    plan_ids = list(
        session.scalars(
            select(TradePlanRow.plan_id).where(
                (TradePlanRow.run_id == run_id) | (TradePlanRow.operation_run_id == run_id)
            )
        )
    )
    related_resources = [run_id, *report_ids, *plan_ids]

    # Children first so foreign keys stay satisfied.  A notification or monitor
    # referencing the run, its reports or its trade plans is part of the cleanup.
    session.execute(delete(NotificationRow).where(NotificationRow.resource_id.in_(related_resources)))
    session.execute(
        delete(BuyEntryMonitorRow).where(
            (BuyEntryMonitorRow.score_run_id == run_id)
            | (BuyEntryMonitorRow.trade_plan_id.in_(plan_ids))
        )
    )
    session.execute(
        delete(TradePlanRow).where(
            (TradePlanRow.run_id == run_id) | (TradePlanRow.operation_run_id == run_id)
        )
    )
    session.execute(delete(ExitAdviceRow).where(ExitAdviceRow.operation_run_id == run_id))
    session.execute(delete(ReportRow).where(ReportRow.run_id == run_id))
    session.execute(delete(PortfolioRow).where(PortfolioRow.run_id == run_id))
    session.execute(delete(ScoreRow).where(ScoreRow.run_id == run_id))
    session.execute(delete(CandidateRow).where(CandidateRow.run_id == run_id))
    session.execute(delete(EvidenceRow).where(EvidenceRow.run_id == run_id))
    session.execute(delete(AgentCall).where(AgentCall.run_id == run_id))
    session.execute(delete(AuditEvent).where(AuditEvent.run_id == run_id))

    # Preserve the immutable data-lake manifests and backtest records; only the
    # run reference is detached so their content stays queryable by object.
    session.execute(
        update(BacktestRun).where(BacktestRun.run_id == run_id).values(run_id=None)
    )
    session.execute(
        update(SnapshotManifestRow).where(SnapshotManifestRow.run_id == run_id).values(run_id=None)
    )

    session.delete(run)
    session.flush()
