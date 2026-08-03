"""Migrate legacy 15-second market refresh accounts to the 5-second default.

Revision ID: 0026_market_refresh_default
Revises: 0025_ai_chat_compaction
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0026_market_refresh_default"
down_revision = "0025_ai_chat_compaction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_refresh_migrations",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("original_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("migrated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    migrated_at = datetime.now(UTC)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO market_refresh_migrations
                (user_id, original_interval_seconds, migrated_at)
            SELECT user_id, market_refresh_interval_seconds, :migrated_at
            FROM user_asset_states
            WHERE market_refresh_interval_seconds = 15
            """
        ),
        {"migrated_at": migrated_at},
    )
    bind.execute(
        sa.text(
            """
            UPDATE user_asset_states
            SET market_refresh_interval_seconds = 5
            WHERE market_refresh_interval_seconds = 15
            """
        )
    )
    with op.batch_alter_table("user_asset_states") as batch:
        batch.alter_column(
            "market_refresh_interval_seconds",
            existing_type=sa.Integer(),
            server_default="5",
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Restore only rows that still have the migrated value and whose account has
    # not been edited after the migration. Other account changes remain intact.
    bind.execute(
        sa.text(
            """
            UPDATE user_asset_states
            SET market_refresh_interval_seconds = 15
            WHERE market_refresh_interval_seconds = 5
              AND user_id IN (
                  SELECT user_id
                  FROM market_refresh_migrations
                  WHERE original_interval_seconds = 15
              )
              AND updated_at <= (
                  SELECT migrated_at
                  FROM market_refresh_migrations
                  WHERE market_refresh_migrations.user_id = user_asset_states.user_id
              )
            """
        )
    )
    with op.batch_alter_table("user_asset_states") as batch:
        batch.alter_column(
            "market_refresh_interval_seconds",
            existing_type=sa.Integer(),
            server_default="15",
        )
    op.drop_table("market_refresh_migrations")
