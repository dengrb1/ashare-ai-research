"""Exit advice, paid-model cache, and persistent AI chat.

Revision ID: 0011_exit_advice_ai_chat
Revises: 0010_research_trade_plans
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_exit_advice_ai_chat"
down_revision = "0010_research_trade_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_asset_states",
        sa.Column("exit_monitor_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "user_asset_states",
        sa.Column("default_profit_trigger", sa.Numeric(18, 2), nullable=True),
    )
    op.create_table(
        "exit_advice",
        sa.Column("advice_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(9), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("action", sa.String(16), nullable=True),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("unrealized_profit", sa.Numeric(18, 2), nullable=False),
        sa.Column("trigger_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("position_snapshot", sa.JSON(), nullable=False),
        sa.Column("research_context", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("reasoning_effort", sa.String(16), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("response_sha256", sa.String(64), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("advice_id"),
        sa.UniqueConstraint("input_hash", name="uq_exit_advice_input_hash"),
    )
    op.create_index("ix_exit_advice_user_id", "exit_advice", ["user_id"])
    op.create_index("ix_exit_advice_symbol", "exit_advice", ["symbol"])
    op.create_index("ix_exit_advice_user_created", "exit_advice", ["user_id", "created_at"])

    op.create_table(
        "ai_response_cache",
        sa.Column("cache_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response_sha256", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("reasoning_effort", sa.String(16), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("cache_id"),
        sa.UniqueConstraint(
            "user_id", "purpose", "request_sha256", name="uq_ai_cache_user_purpose_request"
        ),
    )
    op.create_index("ix_ai_cache_expires", "ai_response_cache", ["expires_at"])

    op.create_table(
        "ai_chat_threads",
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_index("ix_ai_chat_threads_user_id", "ai_chat_threads", ["user_id"])
    op.create_index(
        "ix_ai_chat_thread_user_updated", "ai_chat_threads", ["user_id", "updated_at"]
    )
    op.create_table(
        "ai_chat_messages",
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("mentioned_symbols", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("reasoning_effort", sa.String(16), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("context_sha256", sa.String(64), nullable=True),
        sa.Column("response_sha256", sa.String(64), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["ai_chat_threads.thread_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index("ix_ai_chat_messages_thread_id", "ai_chat_messages", ["thread_id"])
    op.create_index(
        "ix_ai_chat_message_thread_created", "ai_chat_messages", ["thread_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("ai_chat_messages")
    op.drop_table("ai_chat_threads")
    op.drop_table("ai_response_cache")
    op.drop_table("exit_advice")
    op.drop_column("user_asset_states", "default_profit_trigger")
    op.drop_column("user_asset_states", "exit_monitor_enabled")
