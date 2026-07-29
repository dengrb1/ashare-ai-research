"""Add user-owned unified trade-advice monitors.

Revision ID: 0024_trade_advice_monitors
Revises: 0023_mobile_push_devices
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_trade_advice_monitors"
down_revision = "0023_mobile_push_devices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_advice_monitors",
        sa.Column("monitor_id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("user_accounts.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(9), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("manual_buy_price", sa.Numeric(18, 6)),
        sa.Column("manual_sell_price", sa.Numeric(18, 6)),
        sa.Column("ai_buy_price", sa.Numeric(18, 6)),
        sa.Column("ai_sell_price", sa.Numeric(18, 6)),
        sa.Column("stop_loss_price", sa.Numeric(18, 6)),
        sa.Column("rationale", sa.JSON(), nullable=False),
        sa.Column("generated_for", sa.Date()),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("model_name", sa.String(128)),
        sa.Column("model_source", sa.String(16)),
        sa.Column("model_config_sha256", sa.String(64)),
        sa.Column("last_alert_at", sa.DateTime(timezone=True)),
        sa.Column("last_alert_types", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(48)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "symbol", name="uq_trade_advice_monitor_user_symbol"),
    )
    op.create_index(
        "ix_trade_advice_monitor_enabled", "trade_advice_monitors", ["enabled", "symbol"]
    )


def downgrade() -> None:
    op.drop_index("ix_trade_advice_monitor_enabled", table_name="trade_advice_monitors")
    op.drop_table("trade_advice_monitors")
