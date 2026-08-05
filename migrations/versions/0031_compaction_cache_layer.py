"""Record the cache layer used to create a chat compaction checkpoint."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_compaction_cache_layer"
down_revision = "0030_edge_gateway_validation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_chat_compactions",
        sa.Column("cache_layer", sa.String(length=16), nullable=False, server_default="MISS"),
    )


def downgrade() -> None:
    op.drop_column("ai_chat_compactions", "cache_layer")
