"""Add the manually recorded account total for user holdings.

Revision ID: 0009_user_asset_total_assets
Revises: 0008_native_app_sessions
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_user_asset_total_assets"
down_revision = "0008_native_app_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_asset_states") as batch:
        batch.add_column(sa.Column("total_assets", sa.Numeric(18, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_asset_states") as batch:
        batch.drop_column("total_assets")
