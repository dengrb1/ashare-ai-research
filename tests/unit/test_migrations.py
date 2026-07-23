from __future__ import annotations

from importlib import import_module
from unittest.mock import Mock

from sqlalchemy import create_engine, inspect, text

from ashare_ai.cli import migrate_database
from ashare_ai.core.config import get_settings


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
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
            assert revision == "0017_chat_risk_notifications"
            assert {
                "exit_advice",
                "ai_response_cache",
                "ai_chat_threads",
                "ai_chat_messages",
                "ai_chat_attachments",
                "personal_archive_jobs",
                "ai_chat_metrics",
                "notifications",
                "buy_entry_monitors",
            } <= tables
        finally:
            engine.dispose()
    finally:
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
