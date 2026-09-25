"""Remove retired edge storage and make research reports structured metadata."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0034_research_only_cleanup"
down_revision = "0033_model_provider_fallback"
branch_labels = None
depends_on = None

_RETIRED_PUBLIC_SETTING_FIELDS = frozenset(
    {
        "research_execution_mode",
        "edge_gateway_enabled",
        "edge_domain",
        "edge_acme_email",
        "edge_acme_ca_server",
        "edge_frpc_enabled",
        "edge_frpc_config_file",
        "searxng_base_url",
        "searxng_timeout_seconds",
        "searxng_max_results",
        "financial_search_cache_seconds",
        "financial_search_max_concurrency",
        "financial_search_rate_limit_per_minute",
    }
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "active_edge_gateway_configuration" in tables:
        op.drop_table("active_edge_gateway_configuration")
    if "edge_gateway_configuration_versions" in tables:
        op.drop_table("edge_gateway_configuration_versions")

    # Older system-setting revisions could persist retired topology, search,
    # and edge-gateway keys in their JSON payload. Remove them before the
    # runtime stops accepting those fields, while preserving every other
    # audited override.
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
            cleaned_values = {
                key: value
                for key, value in values.items()
                if key not in _RETIRED_PUBLIC_SETTING_FIELDS
            }
            if cleaned_values == values:
                continue
            connection.execute(
                settings_table.update()
                .where(settings_table.c.configuration_id == row["configuration_id"])
                .values(public_values=cleaned_values)
            )

    if "reports" in tables:
        with op.batch_alter_table("reports") as batch:
            batch.alter_column(
                "object_uri",
                existing_type=sa.Text(),
                nullable=True,
            )
            batch.alter_column(
                "content_sha256",
                existing_type=sa.String(length=64),
                nullable=True,
            )
            if "result" not in {column["name"] for column in inspector.get_columns("reports")}:
                batch.add_column(sa.Column("result", sa.JSON(), nullable=True))

    if "model_configuration_versions" in tables:
        model_columns = {
            column["name"]
            for column in inspector.get_columns("model_configuration_versions")
        }
        with op.batch_alter_table("model_configuration_versions") as batch:
            if "search_model" in model_columns:
                batch.drop_column("search_model")
            if "search_reasoning_effort" in model_columns:
                batch.drop_column("search_reasoning_effort")


def downgrade() -> None:
    # Historical edge tables intentionally remain removed from the forward
    # schema. Restoring them would require encrypted configuration data that the
    # research-only runtime no longer understands.
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "reports" in tables:
        columns = {column["name"] for column in inspector.get_columns("reports")}
        with op.batch_alter_table("reports") as batch:
            if "result" in columns:
                batch.drop_column("result")
            if "object_uri" in columns:
                batch.alter_column(
                    "object_uri",
                    existing_type=sa.Text(),
                    nullable=False,
                )
            if "content_sha256" in columns:
                batch.alter_column(
                    "content_sha256",
                    existing_type=sa.String(length=64),
                    nullable=False,
                )
    if "model_configuration_versions" in tables:
        columns = {column["name"] for column in inspector.get_columns("model_configuration_versions")}
        with op.batch_alter_table("model_configuration_versions") as batch:
            if "search_model" not in columns:
                batch.add_column(sa.Column("search_model", sa.String(length=128), nullable=True))
            if "search_reasoning_effort" not in columns:
                batch.add_column(sa.Column("search_reasoning_effort", sa.String(length=16), nullable=True))
