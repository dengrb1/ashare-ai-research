"""Chat lifecycle, encrypted attachments/archives, and price triggers.

Revision ID: 0013_chat_archives_trigger_price
Revises: 0012_api_idempotency_keys
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op

revision = "0013_chat_archives_trigger_price"
down_revision = "0012_api_idempotency_keys"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def _migrate_position_triggers() -> None:
    bind = op.get_bind()
    table = sa.table(
        "user_asset_states",
        sa.column("user_id", sa.String()),
        sa.column("positions", sa.JSON()),
    )
    for user_id, raw_positions in bind.execute(sa.select(table.c.user_id, table.c.positions)):
        changed = False
        positions: list[object] = list(raw_positions or [])
        migrated: list[object] = []
        for raw_position in positions:
            if not isinstance(raw_position, dict):
                migrated.append(raw_position)
                continue
            position = dict(raw_position)
            if position.get("exit_trigger_price") not in (None, ""):
                migrated.append(position)
                continue
            amount = position.get("profit_trigger_amount")
            if amount in (None, ""):
                migrated.append(position)
                continue
            try:
                cost = Decimal(str(position.get("cost")))
                quantity = Decimal(str(position.get("quantity")))
                legacy_amount = Decimal(str(amount))
                if cost <= 0 or quantity <= 0 or legacy_amount <= 0:
                    raise InvalidOperation
                trigger_price = cost + legacy_amount / quantity
                position["exit_trigger_price"] = format(
                    trigger_price.quantize(Decimal("0.000001")), "f"
                )
                changed = True
            except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
                logger.warning(
                    "legacy position trigger could not be converted; preserving amount rule",
                    extra={"user_id": user_id},
                )
            migrated.append(position)
        if changed:
            bind.execute(
                table.update().where(table.c.user_id == user_id).values(positions=migrated)
            )


def upgrade() -> None:
    op.add_column(
        "exit_advice",
        sa.Column(
            "trigger_type",
            sa.String(24),
            server_default="PROFIT_AMOUNT",
            nullable=False,
        ),
    )
    op.add_column("exit_advice", sa.Column("trigger_price", sa.Numeric(18, 6)))

    op.add_column(
        "ai_chat_threads",
        sa.Column("group_mode", sa.String(16), server_default="AUTO", nullable=False),
    )
    op.add_column(
        "ai_chat_threads",
        sa.Column("group_type", sa.String(16), server_default="GENERAL", nullable=False),
    )
    op.add_column("ai_chat_threads", sa.Column("group_label", sa.String(128)))
    op.add_column(
        "ai_chat_threads",
        sa.Column("cumulative_mentions", sa.JSON(), server_default="[]", nullable=False),
    )
    op.add_column("ai_chat_threads", sa.Column("pinned_at", sa.DateTime(timezone=True)))
    op.add_column("ai_chat_threads", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_ai_chat_thread_management",
        "ai_chat_threads",
        ["user_id", "archived_at", "pinned_at", "updated_at", "thread_id"],
    )

    op.add_column(
        "ai_chat_messages",
        sa.Column("status", sa.String(16), server_default="COMPLETED", nullable=False),
    )
    op.add_column(
        "ai_chat_messages",
        sa.Column("parent_message_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "ai_chat_messages", sa.Column("idempotency_key_sha256", sa.String(64))
    )
    op.add_column("ai_chat_messages", sa.Column("request_sha256", sa.String(64)))
    op.add_column(
        "ai_chat_messages",
        sa.Column("mention_refs", sa.JSON(), server_default="[]", nullable=False),
    )
    op.add_column(
        "ai_chat_messages",
        sa.Column("attachment_ids", sa.JSON(), server_default="[]", nullable=False),
    )
    op.add_column("ai_chat_messages", sa.Column("error_code", sa.String(48)))
    op.add_column("ai_chat_messages", sa.Column("request_id", sa.String(64)))
    op.create_foreign_key(
        "fk_ai_chat_message_parent",
        "ai_chat_messages",
        "ai_chat_messages",
        ["parent_message_id"],
        ["message_id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_ai_chat_message_idempotency",
        "ai_chat_messages",
        ["thread_id", "idempotency_key_sha256"],
    )

    op.create_table(
        "ai_chat_attachments",
        sa.Column("attachment_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36)),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("encrypted_object_uri", sa.Text(), nullable=False),
        sa.Column("thumbnail_object_uri", sa.Text()),
        sa.Column("model_object_uri", sa.Text()),
        sa.Column("encryption_key_id", sa.String(64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("deletion_reason", sa.String(32)),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["ai_chat_threads.thread_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attachment_id"),
    )
    op.create_index(
        "ix_ai_chat_attachment_expiry", "ai_chat_attachments", ["expires_at", "deleted_at"]
    )
    op.create_index(
        "ix_ai_chat_attachment_user", "ai_chat_attachments", ["user_id", "uploaded_at"]
    )

    op.create_table(
        "personal_archive_jobs",
        sa.Column("archive_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("encrypted_secret", sa.Text()),
        sa.Column("source_object_uri", sa.Text()),
        sa.Column("output_object_uri", sa.Text()),
        sa.Column("output_sha256", sa.String(64)),
        sa.Column("source_archive_id", sa.String(36)),
        sa.Column("merge_options", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("error_code", sa.String(48)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_archive_id"], ["personal_archive_jobs.archive_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("archive_id"),
    )
    op.create_index(
        "ix_personal_archive_user_created",
        "personal_archive_jobs",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_personal_archive_expiry",
        "personal_archive_jobs",
        ["expires_at", "deleted_at"],
    )

    _migrate_position_triggers()


def downgrade() -> None:
    op.drop_table("personal_archive_jobs")
    op.drop_table("ai_chat_attachments")
    op.drop_constraint("uq_ai_chat_message_idempotency", "ai_chat_messages", type_="unique")
    op.drop_constraint("fk_ai_chat_message_parent", "ai_chat_messages", type_="foreignkey")
    for column in (
        "request_id",
        "error_code",
        "attachment_ids",
        "mention_refs",
        "request_sha256",
        "idempotency_key_sha256",
        "parent_message_id",
        "status",
    ):
        op.drop_column("ai_chat_messages", column)
    op.drop_index("ix_ai_chat_thread_management", table_name="ai_chat_threads")
    for column in (
        "archived_at",
        "pinned_at",
        "cumulative_mentions",
        "group_label",
        "group_type",
        "group_mode",
    ):
        op.drop_column("ai_chat_threads", column)
    op.drop_column("exit_advice", "trigger_price")
    op.drop_column("exit_advice", "trigger_type")
