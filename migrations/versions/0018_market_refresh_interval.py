"""Add a per-account live-market refresh interval.

Revision ID: 0018_market_refresh_interval
Revises: 0017_chat_risk_notifications
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_market_refresh_interval"
down_revision = "0017_chat_risk_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_asset_states") as batch:
        batch.add_column(
            sa.Column(
                "market_refresh_interval_seconds",
                sa.Integer(),
                nullable=False,
                server_default="15",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user_asset_states") as batch:
        batch.drop_column("market_refresh_interval_seconds")
