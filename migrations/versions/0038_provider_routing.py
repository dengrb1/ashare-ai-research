"""Persist provider priority and scenario routing."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_provider_routing"
down_revision = "0037_encrypted_provider_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.add_column(sa.Column("primary_provider_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("fallback_provider_ids", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("routing", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.drop_column("routing")
        batch.drop_column("fallback_provider_ids")
        batch.drop_column("primary_provider_id")
