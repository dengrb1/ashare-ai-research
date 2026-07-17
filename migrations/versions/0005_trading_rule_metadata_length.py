"""Allow versioned trading-rule metadata identifiers.

Revision ID: 0005_rule_metadata_length
Revises: 0004_trading_rule_id_length
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_rule_metadata_length"
down_revision = "0004_trading_rule_id_length"
branch_labels = None
depends_on = None

COLUMN_LENGTHS = {
    "rule_version": (32, 64),
    "source_type": (32, 64),
}


def _column_lengths() -> dict[str, int | None]:
    inspector = sa.inspect(op.get_bind())
    return {
        item["name"]: getattr(item["type"], "length", None)
        for item in inspector.get_columns("trading_rules")
        if item["name"] in COLUMN_LENGTHS
    }


def upgrade() -> None:
    current = _column_lengths()
    changes = [
        (name, length, target)
        for name, (_, target) in COLUMN_LENGTHS.items()
        if (length := current.get(name)) is not None and length < target
    ]
    if not changes:
        return
    with op.batch_alter_table("trading_rules") as batch:
        for name, length, target in changes:
            batch.alter_column(
                name,
                existing_type=sa.String(length),
                type_=sa.String(target),
                existing_nullable=name == "source_type",
            )


def downgrade() -> None:
    current = _column_lengths()
    changes = [
        (name, length, legacy)
        for name, (legacy, _) in COLUMN_LENGTHS.items()
        if (length := current.get(name)) is not None and length > legacy
    ]
    if not changes:
        return
    with op.batch_alter_table("trading_rules") as batch:
        for name, length, legacy in changes:
            batch.alter_column(
                name,
                existing_type=sa.String(length),
                type_=sa.String(legacy),
                existing_nullable=name == "source_type",
            )
