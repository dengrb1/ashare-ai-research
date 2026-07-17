"""Add encrypted immutable model configuration revisions.

Revision ID: 0006_model_configuration
Revises: 0005_rule_metadata_length
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_model_configuration"
down_revision = "0005_rule_metadata_length"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_configuration_versions",
        sa.Column("configuration_id", sa.String(length=36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("encryption_key_id", sa.String(length=64), nullable=False),
        sa.Column("search_model", sa.String(length=128), nullable=False),
        sa.Column("search_reasoning_effort", sa.String(length=16), nullable=False),
        sa.Column("research_model", sa.String(length=128), nullable=False),
        sa.Column("research_reasoning_effort", sa.String(length=16), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["user_accounts.user_id"]),
        sa.UniqueConstraint("version", name="uq_model_configuration_version"),
        sa.UniqueConstraint("config_sha256", name="uq_model_configuration_hash"),
    )
    op.create_table(
        "active_model_configuration",
        sa.Column("scope", sa.String(length=32), primary_key=True),
        sa.Column("configuration_id", sa.String(length=36), nullable=False),
        sa.Column("activated_by", sa.String(length=36), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_status", sa.String(length=16), nullable=False),
        sa.Column("last_check_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["model_configuration_versions.configuration_id"]
        ),
        sa.ForeignKeyConstraint(["activated_by"], ["user_accounts.user_id"]),
    )


def downgrade() -> None:
    op.drop_table("active_model_configuration")
    op.drop_table("model_configuration_versions")
