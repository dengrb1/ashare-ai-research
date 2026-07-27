"""Add immutable persisted administrator system settings.

Revision ID: 0021_system_configuration_center
Revises: 0020_operation_job_runs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0021_system_configuration_center"
down_revision = "0020_operation_job_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_configuration_versions",
        sa.Column("configuration_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("public_values", sa.JSON(), nullable=False),
        sa.Column("encrypted_secret_values", sa.Text(), nullable=True),
        sa.Column("encryption_key_id", sa.String(length=64), nullable=True),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["user_accounts.user_id"]),
        sa.PrimaryKeyConstraint("configuration_id"),
        sa.UniqueConstraint("version", name="uq_system_configuration_version"),
        sa.UniqueConstraint("config_sha256", name="uq_system_configuration_hash"),
    )
    op.create_table(
        "active_system_configuration",
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("configuration_id", sa.String(length=36), nullable=False),
        sa.Column("activated_by", sa.String(length=36), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["system_configuration_versions.configuration_id"]
        ),
        sa.ForeignKeyConstraint(["activated_by"], ["user_accounts.user_id"]),
        sa.PrimaryKeyConstraint("scope"),
    )


def downgrade() -> None:
    op.drop_table("active_system_configuration")
    op.drop_table("system_configuration_versions")
