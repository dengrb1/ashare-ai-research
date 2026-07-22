"""Add explicit point-in-time coordinates to chat messages.

Revision ID: 0014_chat_message_pit
Revises: 0013_chat_archives_trigger_price
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_chat_message_pit"
down_revision = "0013_chat_archives_trigger_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_chat_messages",
        sa.Column("trading_date", sa.Date(), server_default=sa.text("CURRENT_DATE")),
    )
    op.add_column(
        "ai_chat_messages",
        sa.Column(
            "decision_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "ai_chat_messages",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE ai_chat_messages SET decision_at = created_at, "
            "available_at = created_at, "
            "trading_date = CAST(created_at AT TIME ZONE 'Asia/Shanghai' AS DATE)"
        )
    )
    op.alter_column("ai_chat_messages", "trading_date", nullable=False)
    op.alter_column("ai_chat_messages", "decision_at", nullable=False)
    op.alter_column("ai_chat_messages", "available_at", nullable=False)
    op.alter_column("ai_chat_messages", "trading_date", server_default=None)
    op.alter_column("ai_chat_messages", "decision_at", server_default=None)
    op.alter_column("ai_chat_messages", "available_at", server_default=None)


def downgrade() -> None:
    op.drop_column("ai_chat_messages", "available_at")
    op.drop_column("ai_chat_messages", "decision_at")
    op.drop_column("ai_chat_messages", "trading_date")
