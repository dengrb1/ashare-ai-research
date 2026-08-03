from __future__ import annotations

from importlib import import_module
from unittest.mock import Mock

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, text

from ashare_ai.cli import migrate_database
from ashare_ai.core.config import get_settings, runtime_resource_path
from ashare_ai.storage.models import Base


def test_migration_graph_accepts_legacy_cache_head() -> None:
    config = Config(str(runtime_resource_path("alembic.ini")))
    config.set_main_option("script_location", str(runtime_resource_path("migrations")))
    script = ScriptDirectory.from_config(config)
    current = script.get_revision("0027_ai_cache_singleflight")
    legacy = script.get_revision("0026_ai_cache_singleflight")
    assert current is not None
    assert legacy is not None
    assert set(current._normalized_down_revisions) == {
        "0026_market_refresh_default",
        "0026_ai_cache_singleflight",
    }


def test_cli_migrate_bootstraps_empty_database_at_alembic_head(tmp_path, monkeypatch) -> None:
    database = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    try:
        assert migrate_database() == "bootstrapped"
        engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
        try:
            tables = set(inspect(engine).get_table_names())
            assert "trade_plans" in tables
            assert "user_research_preferences" in tables
            assert "automatic_research_report_configs" in tables
            assert "api_idempotency_keys" in tables
            assert "push_devices" in tables
            assert "push_deliveries" in tables
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                assert revision == "0027_ai_cache_singleflight"
            assert {
                "exit_advice",
                "ai_response_cache",
                "ai_chat_threads",
                "ai_chat_messages",
                "ai_chat_compactions",
                "ai_chat_attachments",
                "personal_archive_jobs",
                "ai_chat_metrics",
                "notifications",
                "buy_entry_monitors",
                "system_configuration_versions",
                "active_system_configuration",
            } <= tables
            assert "compacted_history_sha256" in {
                column["name"] for column in inspect(engine).get_columns("ai_chat_messages")
            }
            assert "market_refresh_migrations" in tables
            refresh_column = next(
                column
                for column in inspect(engine).get_columns("user_asset_states")
                if column["name"] == "market_refresh_interval_seconds"
            )
            assert str(refresh_column["default"]).strip("()'") == "5"
            assert "last_singleflight_wait_ms" in {
                column["name"] for column in inspect(engine).get_columns("ai_response_cache")
            }
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()


def test_cache_cost_migration_upgrades_and_downgrades(tmp_path, monkeypatch) -> None:
    database = tmp_path / "cache-governance.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    config = Config(str(runtime_resource_path("alembic.ini")))
    config.set_main_option("script_location", str(runtime_resource_path("migrations")))
    try:
        engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
        try:
            metadata = MetaData()
            for table_name in (
                "model_configuration_versions",
                "agent_calls",
                "ai_response_cache",
                "ai_chat_messages",
            ):
                Table(table_name, metadata, Column("row_id", Integer, primary_key=True))
            metadata.create_all(engine)
            command.stamp(config, "0018_market_refresh_interval")
            assert "model_profiles" not in {
                column["name"]
                for column in inspect(engine).get_columns("model_configuration_versions")
            }
            command.upgrade(config, "0019_model_cache_cost")
            expected = {
                "model_configuration_versions": {"model_profiles"},
                "agent_calls": {
                    "cached_input_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "cache_policy",
                },
                "ai_response_cache": {
                    "cached_input_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "cache_policy",
                },
                "ai_chat_messages": {
                    "cached_input_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "cache_policy",
                    "context_budget_status",
                    "private_context_snapshot",
                },
            }
            for table, columns in expected.items():
                assert columns <= {column["name"] for column in inspect(engine).get_columns(table)}
            command.downgrade(config, "0018_market_refresh_interval")
            for table, columns in expected.items():
                assert not columns & {
                    column["name"] for column in inspect(engine).get_columns(table)
                }
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()


def test_operation_run_migration_upgrades_and_downgrades_on_sqlite(tmp_path, monkeypatch) -> None:
    database = tmp_path / "operation-runs.db"
    url = f"sqlite+pysqlite:///{database.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(runtime_resource_path("alembic.ini")))
    config.set_main_option("script_location", str(runtime_resource_path("migrations")))
    engine = create_engine(url)
    try:
        Base.metadata.create_all(engine)
        command.stamp(config, "0020_operation_job_runs")
        command.downgrade(config, "0019_model_cache_cost")
        assert "operation_run_id" not in {
            column["name"] for column in inspect(engine).get_columns("trade_plans")
        }
        command.upgrade(config, "0020_operation_job_runs")
        for table in ("trade_plans", "exit_advice"):
            assert "operation_run_id" in {
                column["name"] for column in inspect(engine).get_columns(table)
            }
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_scoring_v2_migration_widens_formula_and_backfills_base_score(monkeypatch) -> None:
    migration = import_module(
        "migrations.versions.0010_research_preferences_trade_plans_scoring_v2"
    )
    fake_op = Mock()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    alter_call = fake_op.alter_column.call_args
    assert alter_call.args[:2] == ("scores", "formula_version")
    assert alter_call.kwargs["type_"].length == 64
    fake_op.execute.assert_called_once_with(
        "UPDATE scores SET base_total_score = total_score"
    )
