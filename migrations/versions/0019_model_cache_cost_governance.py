"""Add model cache profiles and token-cost audit fields.

Revision ID: 0019_model_cache_cost
Revises: 0018_market_refresh_interval
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_model_cache_cost"
down_revision = "0018_market_refresh_interval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.add_column(
            sa.Column("model_profiles", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
    with op.batch_alter_table("agent_calls") as batch:
        batch.add_column(
            sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "cache_policy", sa.String(length=16), nullable=False, server_default="COMPATIBLE"
            )
        )
    with op.batch_alter_table("ai_response_cache") as batch:
        batch.add_column(
            sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "cache_policy", sa.String(length=16), nullable=False, server_default="COMPATIBLE"
            )
        )
    with op.batch_alter_table("ai_chat_messages") as batch:
        batch.add_column(
            sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "cache_policy", sa.String(length=16), nullable=False, server_default="COMPATIBLE"
            )
        )
        batch.add_column(
            sa.Column(
                "context_budget_status",
                sa.String(length=32),
                nullable=False,
                server_default="WITHIN_BUDGET",
            )
        )
        batch.add_column(sa.Column("private_context_snapshot", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ai_chat_messages") as batch:
        batch.drop_column("private_context_snapshot")
        batch.drop_column("context_budget_status")
        batch.drop_column("cache_policy")
        batch.drop_column("reasoning_tokens")
        batch.drop_column("cache_write_tokens")
        batch.drop_column("cached_input_tokens")
    with op.batch_alter_table("ai_response_cache") as batch:
        batch.drop_column("cache_policy")
        batch.drop_column("reasoning_tokens")
        batch.drop_column("cache_write_tokens")
        batch.drop_column("cached_input_tokens")
    with op.batch_alter_table("agent_calls") as batch:
        batch.drop_column("cache_policy")
        batch.drop_column("reasoning_tokens")
        batch.drop_column("cache_write_tokens")
        batch.drop_column("cached_input_tokens")
    with op.batch_alter_table("model_configuration_versions") as batch:
        batch.drop_column("model_profiles")
