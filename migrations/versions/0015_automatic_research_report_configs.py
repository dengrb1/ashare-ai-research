"""Add independent A/B automatic research report settings.

Revision ID: 0015_auto_report_configs
Revises: 0014_chat_message_pit
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_auto_report_configs"
down_revision = "0014_chat_message_pit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automatic_research_report_configs",
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("user_accounts.user_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("slot", sa.String(length=1), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="MARKET"),
        sa.Column("symbols", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("total_budget", sa.Numeric(18, 2), nullable=False, server_default="1000000"),
        sa.Column("per_symbol_budget", sa.Numeric(18, 2), nullable=False, server_default="80000"),
        sa.Column("max_stock_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "slot", name="uq_automatic_research_user_slot"),
    )
    op.execute(
        sa.text(
            "INSERT INTO automatic_research_report_configs "
            "(user_id, slot, enabled, scope, symbols, total_budget, per_symbol_budget, "
            "config_version, updated_at) "
            "SELECT user_id, 'A', auto_enabled, 'MARKET', '[]', 1000000, 80000, 1, updated_at "
            "FROM user_research_preferences"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO automatic_research_report_configs "
            "(user_id, slot, enabled, scope, symbols, total_budget, per_symbol_budget, "
            "config_version, updated_at) "
            "SELECT user_id, 'B', false, 'MARKET', '[]', 1000000, 80000, 1, updated_at "
            "FROM user_research_preferences"
        )
    )


def downgrade() -> None:
    op.drop_table("automatic_research_report_configs")
