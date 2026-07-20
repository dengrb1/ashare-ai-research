"""Persist native-client idempotency keys for asynchronous submissions.

Revision ID: 0012_api_idempotency_keys
Revises: 0011_exit_advice_ai_chat
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_api_idempotency_keys"
down_revision = "0011_exit_advice_ai_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_idempotency_keys",
        sa.Column("idempotency_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("route", sa.String(160), nullable=False),
        sa.Column("key_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("idempotency_id"),
        sa.UniqueConstraint("user_id", "route", "key_sha256", name="uq_api_idempotency_scope"),
    )
    op.create_index(
        "ix_api_idempotency_created", "api_idempotency_keys", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("api_idempotency_keys")
