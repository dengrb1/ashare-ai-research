"""Add sanitized model probe diagnostics."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_model_probe_logs"
down_revision = "0028_edge_gateway_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_probe_logs",
        sa.Column("log_id", sa.String(length=36), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("endpoint_path", sa.String(length=128), nullable=False),
        sa.Column("request_mode", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("header_presence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index("ix_model_probe_logs_created", "model_probe_logs", ["created_at"])
    op.create_index(
        "ix_model_probe_logs_model_created",
        "model_probe_logs",
        ["model", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_probe_logs_model_created", table_name="model_probe_logs")
    op.drop_index("ix_model_probe_logs_created", table_name="model_probe_logs")
    op.drop_table("model_probe_logs")
