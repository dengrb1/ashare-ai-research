"""Add per-user editable watchlists and simulated positions.

Revision ID: 0007_user_asset_state
Revises: 0006_model_configuration
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_user_asset_state"
down_revision = "0006_model_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_asset_states",
        sa.Column("user_id", sa.String(length=36), primary_key=True),
        sa.Column("watchlist", sa.JSON(), nullable=False),
        sa.Column("positions", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("user_asset_states")
