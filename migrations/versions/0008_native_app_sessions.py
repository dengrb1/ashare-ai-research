"""Add native App token sessions and backtest retry counters.

Revision ID: 0008_native_app_sessions
Revises: 0007_user_asset_state
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_native_app_sessions"
down_revision = "0007_user_asset_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_sessions") as batch:
        batch.add_column(
            sa.Column("session_type", sa.String(length=16), nullable=False, server_default="WEB")
        )
        batch.add_column(sa.Column("refresh_token_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index(
            "ix_user_sessions_refresh_token_hash", ["refresh_token_hash"], unique=True
        )
    op.execute("UPDATE user_sessions SET session_type = 'WEB' WHERE session_type IS NULL")
    with op.batch_alter_table("backtest_runs") as batch:
        batch.add_column(
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("backtest_runs") as batch:
        batch.drop_column("retry_count")
    with op.batch_alter_table("user_sessions") as batch:
        batch.drop_index("ix_user_sessions_refresh_token_hash")
        batch.drop_column("refresh_expires_at")
        batch.drop_column("refresh_token_hash")
        batch.drop_column("session_type")
