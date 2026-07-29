"""Add encrypted mobile push registrations and transactional deliveries.

Revision ID: 0023_mobile_push_devices
Revises: 0022_candidate_industry_name
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_mobile_push_devices"
down_revision = "0022_candidate_industry_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_devices",
        sa.Column("device_id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("user_accounts.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("installation_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("encrypted_registration_id", sa.Text(), nullable=False),
        sa.Column("registration_fingerprint", sa.String(64), nullable=False),
        sa.Column("app_version", sa.String(32)),
        sa.Column("os_version", sa.String(32)),
        sa.Column("device_model", sa.String(96)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "installation_id", name="uq_push_device_installation"),
    )
    op.create_index("ix_push_device_user_active", "push_devices", ["user_id", "disabled_at"])
    op.create_table(
        "push_deliveries",
        sa.Column("delivery_id", sa.String(36), primary_key=True),
        sa.Column(
            "notification_id",
            sa.String(36),
            sa.ForeignKey("notifications.notification_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.String(36),
            sa.ForeignKey("push_devices.device_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(128)),
        sa.Column("error_code", sa.String(48)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("notification_id", "device_id", name="uq_push_delivery_target"),
    )
    op.create_index("ix_push_delivery_due", "push_deliveries", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_push_delivery_due", table_name="push_deliveries")
    op.drop_table("push_deliveries")
    op.drop_index("ix_push_device_user_active", table_name="push_devices")
    op.drop_table("push_devices")
