"""Persist chat compaction checkpoints and their cache lineage.

Revision ID: 0025_ai_chat_compaction
Revises: 0024_trade_advice_monitors
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_ai_chat_compaction"
down_revision = "0024_trade_advice_monitors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_chat_compactions",
        sa.Column("compaction_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "thread_id",
            sa.String(length=36),
            sa.ForeignKey("ai_chat_threads.thread_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("summary_sha256", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("covered_through_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=False),
        sa.Column("model_configuration_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("thread_id", "source_sha256", name="uq_ai_chat_compaction_source"),
    )
    op.create_index(
        "ix_ai_chat_compaction_thread_created",
        "ai_chat_compactions",
        ["thread_id", "created_at"],
    )
    with op.batch_alter_table("ai_chat_messages") as batch:
        batch.add_column(sa.Column("compacted_history_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ai_chat_messages") as batch:
        batch.drop_column("compacted_history_sha256")
    op.drop_index("ix_ai_chat_compaction_thread_created", table_name="ai_chat_compactions")
    op.drop_table("ai_chat_compactions")
