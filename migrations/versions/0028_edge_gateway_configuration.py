"""Add versioned edge gateway configuration."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_edge_gateway_configuration"
down_revision = "0027_ai_cache_singleflight"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "edge_gateway_configuration_versions",
        sa.Column("configuration_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("proxy_hosts", sa.JSON(), nullable=False),
        sa.Column("encrypted_frpc_toml", sa.Text(), nullable=False),
        sa.Column("encryption_key_id", sa.String(length=64), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_sha256", sa.String(length=64), nullable=True),
        sa.Column("apply_status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("apply_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["user_accounts.user_id"]),
        sa.PrimaryKeyConstraint("configuration_id"),
        sa.UniqueConstraint("version", name="uq_edge_gateway_configuration_version"),
        sa.UniqueConstraint("config_sha256", name="uq_edge_gateway_configuration_hash"),
    )
    op.create_table(
        "active_edge_gateway_configuration",
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("configuration_id", sa.String(length=36), nullable=False),
        sa.Column("activated_by", sa.String(length=36), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["activated_by"], ["user_accounts.user_id"]),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["edge_gateway_configuration_versions.configuration_id"]
        ),
        sa.PrimaryKeyConstraint("scope"),
    )


def downgrade() -> None:
    op.drop_table("active_edge_gateway_configuration")
    op.drop_table("edge_gateway_configuration_versions")
