"""Persist the FRP validation mode for edge-gateway revisions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_edge_gateway_validation"
down_revision = "0029_model_probe_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "edge_gateway_configuration_versions",
        sa.Column("validation_mode", sa.String(length=16), nullable=False, server_default="STRICT"),
    )


def downgrade() -> None:
    op.drop_column("edge_gateway_configuration_versions", "validation_mode")
