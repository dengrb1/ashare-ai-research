"""Record shared AI cache single-flight wait time.

Revision ID: 0027_ai_cache_singleflight
Revises: 0026_market_refresh_default, 0026_ai_cache_singleflight
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_ai_cache_singleflight"
down_revision = ("0026_market_refresh_default", "0026_ai_cache_singleflight")
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("ai_response_cache")
    }
    if "last_singleflight_wait_ms" not in columns:
        with op.batch_alter_table("ai_response_cache") as batch:
            batch.add_column(
                sa.Column(
                    "last_singleflight_wait_ms",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("ai_response_cache") as batch:
        batch.drop_column("last_singleflight_wait_ms")
