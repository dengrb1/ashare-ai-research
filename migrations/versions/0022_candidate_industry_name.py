"""Carry the human-readable industry name on candidate rows.

Revision ID: 0022_candidate_industry_name
Revises: 0021_system_configuration_center
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0022_candidate_industry_name"
down_revision = "0021_system_configuration_center"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("candidates") as batch:
        batch.add_column(sa.Column("industry_name", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("candidates") as batch:
        batch.drop_column("industry_name")
