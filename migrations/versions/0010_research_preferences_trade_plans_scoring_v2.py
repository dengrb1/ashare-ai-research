"""Research preferences, trade plans, and scoring v2 fields.

Revision ID: 0010_research_trade_plans
Revises: 0009_user_asset_total_assets
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_research_trade_plans"
down_revision = "0009_user_asset_total_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "scores",
        "formula_version",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_table(
        "user_research_preferences",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("auto_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.add_column(
        "scores",
        sa.Column("base_total_score", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "scores",
        sa.Column("dividend_bonus", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "scores",
        sa.Column("event_risk_multiplier", sa.Float(), server_default="1", nullable=False),
    )
    op.execute("UPDATE scores SET base_total_score = total_score")
    op.create_table(
        "trade_plans",
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("objective", sa.String(length=32), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("budget_override", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("snapshot_ids", sa.JSON(), nullable=False),
        sa.Column("optimizer_version", sa.String(length=64), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("model_configuration", sa.JSON(), nullable=True),
        sa.Column("deterministic_result", sa.JSON(), nullable=True),
        sa.Column("ai_explanation", sa.JSON(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("active_trade_plan_key", sa.String(length=64), nullable=True),
        sa.Column("object_uri", sa.Text(), nullable=True),
        sa.Column("object_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["reports.report_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["job_runs.run_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("active_trade_plan_key", name="uq_active_trade_plan_key"),
    )
    op.create_index("ix_trade_plans_user_id", "trade_plans", ["user_id"])
    op.create_index(
        "ix_trade_plans_active_trade_plan_key",
        "trade_plans",
        ["active_trade_plan_key"],
    )
    op.create_index(
        "ix_trade_plan_user_report_created",
        "trade_plans",
        ["user_id", "report_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trade_plan_user_report_created", table_name="trade_plans")
    op.drop_index("ix_trade_plans_active_trade_plan_key", table_name="trade_plans")
    op.drop_index("ix_trade_plans_user_id", table_name="trade_plans")
    op.drop_table("trade_plans")
    op.drop_column("scores", "event_risk_multiplier")
    op.drop_column("scores", "dividend_bonus")
    op.drop_column("scores", "base_total_score")
    op.drop_table("user_research_preferences")
    op.alter_column(
        "scores",
        "formula_version",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
