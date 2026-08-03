"""Recognize the cache migration head emitted by the pre-merge branch layout.

The old mixed branch used this revision identifier for the same cache column
that is now represented by ``0027_ai_cache_singleflight``.  It follows the
current market-refresh migration so databases stamped with this historical
head can advance directly to the merge revision without replaying the base
chain.  The marker is intentionally a no-op; the merge migration verifies the
column idempotently.
"""

from __future__ import annotations

revision = "0026_ai_cache_singleflight"
down_revision = "0026_market_refresh_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
