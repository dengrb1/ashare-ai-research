"""Add the factor genome version registry and its active pointer.

The registry stores every evolution candidate with its parameters, walk-forward
metrics, dataset hash and LLM proposal hash.  Approving a candidate only moves
the active pointer; historical research results, scores and backtest reports are
never rewritten.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0035_factor_genome_registry"
down_revision = "0034_research_only_cleanup"
branch_labels = None
depends_on = None

# Retired Jev settings that older system-setting revisions may still carry.
_RETIRED_PUBLIC_SETTING_FIELDS = frozenset(
    {
        "jev_model_dir",
        "jev_model_version",
        "jev_device",
        "jev_backend",
        "jev_live_base_url",
        "jev_confidence_threshold",
        "jev_training_enabled",
        "jev_training_weekday",
        "jev_training_hour",
        "jev_training_min_samples",
        "jev_training_max_drawdown",
        "jev_training_max_turnover",
        "jev_training_min_improvement",
        "jev_training_max_candidates",
    }
)
_RETIRED_SECRET_SETTING_FIELDS = frozenset({"jev_live_api_key"})


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())

    if "factor_genome_versions" not in tables:
        op.create_table(
            "factor_genome_versions",
            sa.Column("candidate_id", sa.String(length=64), primary_key=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="CANDIDATE"),
            sa.Column("source", sa.String(length=16), nullable=False, server_default="DETERMINISTIC"),
            sa.Column("formula_version", sa.String(length=32), nullable=False),
            sa.Column("genome", sa.JSON(), nullable=False),
            sa.Column("parameter_sha256", sa.String(length=64), nullable=False),
            sa.Column("metrics", sa.JSON(), nullable=False),
            sa.Column("dataset_sha256", sa.String(length=64), nullable=False),
            sa.Column("proposal_sha256", sa.String(length=64), nullable=True),
            sa.Column("hypothesis", sa.Text(), nullable=True),
            sa.Column("risk_notes", sa.Text(), nullable=True),
            sa.Column("explanation", sa.Text(), nullable=True),
            sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reviewed_by", sa.String(length=36), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(
                ["reviewed_by"], ["user_accounts.user_id"], name="fk_factor_genome_reviewer"
            ),
            sa.UniqueConstraint("candidate_id", name="uq_factor_genome_candidate"),
        )
        op.create_index(
            "ix_factor_genome_status_created",
            "factor_genome_versions",
            ["status", "created_at"],
        )

    if "active_factor_genome" not in tables:
        op.create_table(
            "active_factor_genome",
            sa.Column("scope", sa.String(length=32), primary_key=True, server_default="default"),
            sa.Column("candidate_id", sa.String(length=64), nullable=False),
            sa.Column("previous_candidate_id", sa.String(length=64), nullable=True),
            sa.Column("activated_by", sa.String(length=36), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["candidate_id"],
                ["factor_genome_versions.candidate_id"],
                name="fk_active_factor_genome_candidate",
            ),
            sa.ForeignKeyConstraint(
                ["previous_candidate_id"],
                ["factor_genome_versions.candidate_id"],
                name="fk_active_factor_genome_previous",
            ),
            sa.ForeignKeyConstraint(
                ["activated_by"], ["user_accounts.user_id"], name="fk_active_factor_genome_actor"
            ),
        )

    # Drop retired Jev keys from audited setting revisions so the runtime stops
    # accepting them, while preserving every other override.
    if "system_configuration_versions" in tables:
        settings_table = sa.Table(
            "system_configuration_versions",
            sa.MetaData(),
            autoload_with=connection,
        )
        rows = connection.execute(
            sa.select(
                settings_table.c.configuration_id,
                settings_table.c.public_values,
            )
        ).mappings()
        for row in rows:
            values = row["public_values"]
            if not isinstance(values, dict):
                continue
            cleaned = {
                key: value
                for key, value in values.items()
                if key not in _RETIRED_PUBLIC_SETTING_FIELDS
            }
            if cleaned == values:
                continue
            connection.execute(
                settings_table.update()
                .where(settings_table.c.configuration_id == row["configuration_id"])
                .values(public_values=cleaned)
            )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "active_factor_genome" in tables:
        op.drop_table("active_factor_genome")
    if "factor_genome_versions" in tables:
        op.drop_index("ix_factor_genome_status_created", table_name="factor_genome_versions")
        op.drop_table("factor_genome_versions")
