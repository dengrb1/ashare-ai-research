"""Allow the durable research data-readiness waiting status.

Revision ID: 0016_research_readiness
Revises: 0015_auto_report_configs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_research_readiness"
down_revision = "0015_auto_report_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "job_runs",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "job_runs",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
