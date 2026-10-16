"""Store provider API keys encrypted per provider."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_encrypted_provider_keys"
down_revision = "0036_asset_groups_and_monitor_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.add_column(sa.Column("encrypted_provider_keys", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.drop_column("encrypted_provider_keys")
