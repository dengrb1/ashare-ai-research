"""Persist frozen broad-market context with every deterministic score."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_market_index_score_context"
down_revision = "0031_compaction_cache_layer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scores", sa.Column("market_index_snapshot", sa.JSON(), nullable=True))
    op.add_column(
        "scores",
        sa.Column("market_regime", sa.String(length=16), nullable=False, server_default="UNKNOWN"),
    )
    op.add_column(
        "scores",
        sa.Column("market_score_adjustment", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "scores",
        sa.Column("market_risk_multiplier", sa.Float(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("scores", "market_risk_multiplier")
    op.drop_column("scores", "market_score_adjustment")
    op.drop_column("scores", "market_regime")
    op.drop_column("scores", "market_index_snapshot")
