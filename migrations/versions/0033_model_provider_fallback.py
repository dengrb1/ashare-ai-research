"""Add encrypted ordered model provider fallback chains.

Revision ID: 0033_model_provider_fallback
Revises: 0032_market_index_score_context
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_model_provider_fallback"
down_revision = "0032_market_index_score_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.add_column(
            sa.Column("providers", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )


def downgrade() -> None:
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.drop_column("providers")
