"""Add chat observability, notifications, and paper-monitoring resources.

Revision ID: 0017_chat_risk_notifications
Revises: 0016_research_readiness
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_chat_risk_notifications"
down_revision = "0016_research_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_asset_states") as batch:
        batch.add_column(
            sa.Column("stop_loss_monitor_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(
            sa.Column("buy_monitor_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    with op.batch_alter_table("active_model_configuration") as batch:
        batch.add_column(
            sa.Column("structured_output_supported", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("streaming_supported", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("ai_chat_messages") as batch:
        batch.add_column(
            sa.Column("streaming_mode", sa.String(length=16), nullable=False, server_default="STREAMING")
        )
        batch.add_column(
            sa.Column("data_status", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
        batch.add_column(sa.Column("response_id", sa.String(length=128), nullable=True))
        batch.add_column(
            sa.Column("model_configuration_sha256", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("attachment_context_sha256", sa.String(length=64), nullable=True)
        )

    op.create_table(
        "ai_chat_metrics",
        sa.Column("metric_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("user_accounts.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("bucket_date", sa.Date(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("singleflight_wait_ms_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("degraded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "metric", "bucket_date", name="uq_chat_metric_bucket"),
    )
    op.create_index("ix_chat_metric_user_date", "ai_chat_metrics", ["user_id", "bucket_date"])

    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("user_accounts.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("notification_type", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="INFO"),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("dedupe_key", sa.String(length=64), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_notification_user_dedupe"),
    )
    op.create_index(
        "ix_notification_user_read_created", "notifications", ["user_id", "read_at", "created_at"]
    )
    op.create_index("ix_notification_expiry", "notifications", ["expires_at", "read_at"])

    op.create_table(
        "buy_entry_monitors",
        sa.Column("monitor_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("user_accounts.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=9), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="ACTIVE"),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_low", sa.Numeric(18, 6), nullable=False),
        sa.Column("entry_high", sa.Numeric(18, 6), nullable=False),
        sa.Column("score_run_id", sa.String(length=36), sa.ForeignKey("job_runs.run_id"), nullable=True),
        sa.Column("trade_plan_id", sa.String(length=36), sa.ForeignKey("trade_plans.plan_id"), nullable=True),
        sa.Column("rationale", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "symbol", "effective_date", name="uq_buy_monitor_user_symbol_date"),
    )
    op.create_index(
        "ix_buy_monitor_active", "buy_entry_monitors", ["status", "effective_date", "expires_at"]
    )
    op.create_index("ix_buy_monitor_user_symbol", "buy_entry_monitors", ["user_id", "symbol"])


def downgrade() -> None:
    op.drop_index("ix_buy_monitor_user_symbol", table_name="buy_entry_monitors")
    op.drop_index("ix_buy_monitor_active", table_name="buy_entry_monitors")
    op.drop_table("buy_entry_monitors")
    op.drop_index("ix_notification_expiry", table_name="notifications")
    op.drop_index("ix_notification_user_read_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_chat_metric_user_date", table_name="ai_chat_metrics")
    op.drop_table("ai_chat_metrics")
    with op.batch_alter_table("ai_chat_messages") as batch:
        batch.drop_column("attachment_context_sha256")
        batch.drop_column("model_configuration_sha256")
        batch.drop_column("response_id")
        batch.drop_column("data_status")
        batch.drop_column("streaming_mode")
    with op.batch_alter_table("active_model_configuration") as batch:
        batch.drop_column("streaming_supported")
        batch.drop_column("structured_output_supported")
    with op.batch_alter_table("user_asset_states") as batch:
        batch.drop_column("buy_monitor_enabled")
        batch.drop_column("stop_loss_monitor_enabled")
