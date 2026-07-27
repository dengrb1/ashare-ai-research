"""Add dedicated audit runs for trade plans and exit advice.

Revision ID: 0020
Revises: 0019
"""

from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision = "0020_operation_job_runs"
down_revision = "0019_model_cache_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.add_column(sa.Column("operation_run_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_trade_plans_operation_run", "job_runs", ["operation_run_id"], ["run_id"]
        )
        batch.create_index("ix_trade_plans_operation_run_id", ["operation_run_id"])
    with op.batch_alter_table("exit_advice") as batch:
        batch.add_column(sa.Column("operation_run_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_exit_advice_operation_run", "job_runs", ["operation_run_id"], ["run_id"]
        )
        batch.create_index("ix_exit_advice_operation_run_id", ["operation_run_id"])

    bind = op.get_bind()
    metadata = sa.MetaData()
    jobs = sa.Table("job_runs", metadata, autoload_with=bind)
    plans = sa.Table("trade_plans", metadata, autoload_with=bind)
    advice = sa.Table("exit_advice", metadata, autoload_with=bind)
    for source, resource_column, run_type, trading_date_column in (
        (plans, plans.c.plan_id, "TRADE_PLAN", plans.c.trading_date),
        (advice, advice.c.advice_id, "EXIT_ADVICE", None),
    ):
        rows = bind.execute(sa.select(source).where(source.c.operation_run_id.is_(None))).mappings()
        for row in rows:
            resource_id = str(row[resource_column.name])
            run_id = sha256(f"{run_type}:{resource_id}".encode()).hexdigest()[:36]
            started_at = row.get("started_at") or row.get("created_at") or row["decision_at"]
            completed_at = row.get("completed_at")
            status = str(row.get("status") or "FAILED")
            bind.execute(
                jobs.insert().values(
                    run_id=run_id,
                    user_id=row.get("user_id"),
                    active_research_key=None,
                    run_type=run_type,
                    trading_date=(
                        row[trading_date_column.name]
                        if trading_date_column is not None
                        else row["decision_at"].date()
                    ),
                    decision_at=row["decision_at"],
                    status=status,
                    idempotency_key=sha256(
                        f"historical:{run_type}:{resource_id}".encode()
                    ).hexdigest(),
                    manifest={"historical_backfill": True, "resource_id": resource_id},
                    input_hash=str(
                        row.get("input_hash") or sha256(resource_id.encode()).hexdigest()
                    ),
                    output_hash=row.get("output_hash") or row.get("response_sha256"),
                    started_at=started_at,
                    completed_at=completed_at,
                    error_message=row.get("error_message"),
                )
            )
            bind.execute(
                source.update()
                .where(resource_column == resource_id)
                .values(operation_run_id=run_id)
            )


def downgrade() -> None:
    with op.batch_alter_table("exit_advice") as batch:
        batch.drop_index("ix_exit_advice_operation_run_id")
        batch.drop_column("operation_run_id")
    with op.batch_alter_table("trade_plans") as batch:
        batch.drop_index("ix_trade_plans_operation_run_id")
        batch.drop_column("operation_run_id")
