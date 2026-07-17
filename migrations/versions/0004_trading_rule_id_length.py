"""Allow versioned trading-rule identifiers longer than UUIDs.

Revision ID: 0004_trading_rule_id_length
Revises: 0003_research_snapshots
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_trading_rule_id_length"
down_revision = "0003_research_snapshots"
branch_labels = None
depends_on = None

RULE_ID_LENGTH = 128
LEGACY_RULE_ID_LENGTH = 36


def _rule_id_length() -> int | None:
    inspector = sa.inspect(op.get_bind())
    column = next(
        item for item in inspector.get_columns("trading_rules") if item["name"] == "rule_id"
    )
    return getattr(column["type"], "length", None)


def upgrade() -> None:
    current_length = _rule_id_length()
    if current_length is None or current_length >= RULE_ID_LENGTH:
        return
    with op.batch_alter_table("trading_rules") as batch:
        batch.alter_column(
            "rule_id",
            existing_type=sa.String(current_length),
            type_=sa.String(RULE_ID_LENGTH),
            existing_nullable=False,
        )


def downgrade() -> None:
    current_length = _rule_id_length()
    if current_length is None or current_length <= LEGACY_RULE_ID_LENGTH:
        return
    with op.batch_alter_table("trading_rules") as batch:
        batch.alter_column(
            "rule_id",
            existing_type=sa.String(current_length),
            type_=sa.String(LEGACY_RULE_ID_LENGTH),
            existing_nullable=False,
        )
