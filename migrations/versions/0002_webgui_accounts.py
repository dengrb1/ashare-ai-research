"""Add WebGUI accounts, sessions and run ownership.

Revision ID: 0002_webgui_accounts
Revises: 0001_initial
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_webgui_accounts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    if "user_accounts" in existing_tables:
        return
    op.create_table(
        "user_accounts",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_user_accounts_username", "user_accounts", ["username"], unique=True)
    op.create_table(
        "user_sessions",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("user_accounts.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("user_session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("client_ip", sa.String(64)),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True)
    op.create_index(
        "ix_user_session_active", "user_sessions", ["user_id", "expires_at", "revoked_at"]
    )
    with op.batch_alter_table("job_runs") as batch:
        batch.add_column(sa.Column("user_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_job_runs_user_id", "user_accounts", ["user_id"], ["user_id"])
        batch.create_index("ix_job_runs_user_id", ["user_id"])
    with op.batch_alter_table("backtest_runs") as batch:
        batch.add_column(sa.Column("user_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_backtest_runs_user_id", "user_accounts", ["user_id"], ["user_id"]
        )
        batch.create_index("ix_backtest_runs_user_id", ["user_id"])


def downgrade() -> None:
    with op.batch_alter_table("backtest_runs") as batch:
        batch.drop_index("ix_backtest_runs_user_id")
        batch.drop_constraint("fk_backtest_runs_user_id", type_="foreignkey")
        batch.drop_column("user_id")
    with op.batch_alter_table("job_runs") as batch:
        batch.drop_index("ix_job_runs_user_id")
        batch.drop_constraint("fk_job_runs_user_id", type_="foreignkey")
        batch.drop_column("user_id")
    op.drop_table("user_sessions")
    op.drop_table("user_accounts")
