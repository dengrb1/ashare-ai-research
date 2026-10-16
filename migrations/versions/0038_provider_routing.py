"""Persist provider priority and scenario routing."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_provider_routing"
down_revision = "0037_encrypted_provider_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.add_column(sa.Column("primary_provider_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("fallback_provider_ids", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("routing", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table(
        "update_jobs",
        sa.Column("update_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("current_version", sa.String(length=64), nullable=False),
        sa.Column("target_version", sa.String(length=64), nullable=True),
        sa.Column("update_tag", sa.String(length=96), nullable=False),
        sa.Column("architecture_label", sa.String(length=64), nullable=False),
        sa.Column("manifest_url", sa.Text(), nullable=True),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.Column("backup_path", sa.Text(), nullable=True),
        sa.Column("backup_sha256", sa.String(length=64), nullable=True),
        sa.Column("fallback_active", sa.Boolean(), nullable=False),
        sa.Column("sync_state", sa.JSON(), nullable=False),
        sa.Column("repair_attempts", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"]),
        sa.PrimaryKeyConstraint("update_id"),
        sa.UniqueConstraint("update_tag"),
    )
    op.create_index("ix_update_jobs_status_created", "update_jobs", ["status", "created_at"])
    op.create_index("ix_update_jobs_tag", "update_jobs", ["update_tag"])


def downgrade() -> None:
    op.drop_index("ix_update_jobs_tag", table_name="update_jobs")
    op.drop_index("ix_update_jobs_status_created", table_name="update_jobs")
    op.drop_table("update_jobs")
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.drop_column("routing")
        batch.drop_column("fallback_provider_ids")
        batch.drop_column("primary_provider_id")
