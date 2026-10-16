"""Add grouped watchlists and user monitoring policy."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_asset_groups_and_monitor_policy"
down_revision = "0035_factor_genome_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_asset_states") as batch:
        batch.add_column(sa.Column("watchlist_groups", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("monitoring_policy", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("user_asset_states") as batch:
        batch.drop_column("monitoring_policy")
        batch.drop_column("watchlist_groups")
