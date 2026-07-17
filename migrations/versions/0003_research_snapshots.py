"""Add research ownership to snapshots and active-run uniqueness.

Revision ID: 0003_research_snapshots
Revises: 0002_webgui_accounts
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_research_snapshots"
down_revision = "0002_webgui_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    job_columns = {item["name"] for item in inspector.get_columns("job_runs")}
    snapshot_columns = {item["name"] for item in inspector.get_columns("snapshot_manifests")}
    if "active_research_key" not in job_columns:
        with op.batch_alter_table("job_runs") as batch:
            batch.add_column(sa.Column("active_research_key", sa.String(64), nullable=True))
            batch.create_unique_constraint(
                "uq_job_runs_active_research_key", ["active_research_key"]
            )
            batch.create_index("ix_job_runs_active_research_key", ["active_research_key"])
    if "run_id" not in snapshot_columns:
        with op.batch_alter_table("snapshot_manifests") as batch:
            batch.add_column(sa.Column("run_id", sa.String(36), nullable=True))
            batch.create_foreign_key(
                "fk_snapshot_manifests_run_id", "job_runs", ["run_id"], ["run_id"]
            )
            batch.create_index("ix_snapshot_manifests_run_id", ["run_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    snapshot_columns = {item["name"] for item in inspector.get_columns("snapshot_manifests")}
    job_columns = {item["name"] for item in inspector.get_columns("job_runs")}
    if "run_id" in snapshot_columns:
        with op.batch_alter_table("snapshot_manifests") as batch:
            batch.drop_index("ix_snapshot_manifests_run_id")
            batch.drop_constraint("fk_snapshot_manifests_run_id", type_="foreignkey")
            batch.drop_column("run_id")
    if "active_research_key" in job_columns:
        with op.batch_alter_table("job_runs") as batch:
            batch.drop_index("ix_job_runs_active_research_key")
            batch.drop_constraint("uq_job_runs_active_research_key", type_="unique")
            batch.drop_column("active_research_key")
