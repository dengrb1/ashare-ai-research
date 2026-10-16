from __future__ import annotations

import argparse

import uvicorn
from sqlalchemy import create_engine, inspect

from ashare_ai.core.config import get_settings, runtime_resource_path
from ashare_ai.doctor import format_doctor, run_doctor
from ashare_ai.storage.models import Base, UpdateJob


def migrate_database() -> str:
    """Bootstrap an empty database at head; migrate populated databases revision by revision."""

    from alembic import command
    from alembic.config import Config

    config = Config(str(runtime_resource_path("alembic.ini")))
    config.set_main_option("script_location", str(runtime_resource_path("migrations")))
    engine = create_engine(get_settings().database_url)
    try:
        table_names = set(inspect(engine).get_table_names()) - {"alembic_version"}
        if not table_names:
            Base.metadata.create_all(engine)
            command.stamp(config, "head")
            return "bootstrapped"
        command.upgrade(config, "head")
        # 0038 predates the durable update coordinator in installations that
        # already ran that migration. Keep those databases forward compatible
        # without rewriting the immutable Alembic revision history.
        if "update_jobs" not in inspect(engine).get_table_names():
            UpdateJob.__table__.create(engine, checkfirst=True)
        return "upgraded"
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="ashare-ai")
    subparsers = parser.add_subparsers(dest="command", required=True)
    api = subparsers.add_parser("api", help="Run the FastAPI service")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", default=8000, type=int)
    subparsers.add_parser("migrate", help="Run database migrations")
    doctor = subparsers.add_parser("doctor", help="Run read-only configuration diagnostics")
    doctor.add_argument(
        "--skip-market",
        action="store_true",
        help="Skip the external market connectivity request",
    )

    # Decision commands integration.  ``factor`` is the only decision mode that
    # creates new decisions; ``legacy`` is accepted as a compatibility alias.
    decision = subparsers.add_parser("decision", help="Decision mode commands")
    decision_subs = decision.add_subparsers(dest="decision_command", required=True)

    predict_cmd = decision_subs.add_parser("predict", help="Single prediction")
    predict_cmd.add_argument("symbol", help="Stock symbol")
    predict_cmd.add_argument("--bundle-dir", type=str, help="Bundle directory")
    predict_cmd.add_argument("--mode", default="factor", choices=["factor", "legacy"])

    batch_cmd = decision_subs.add_parser("batch", help="Batch prediction")
    batch_cmd.add_argument("symbols_file", help="File with symbols")
    batch_cmd.add_argument("output_file", help="Output JSON file")
    batch_cmd.add_argument("--bundle-dir", type=str, help="Bundle directory")
    batch_cmd.add_argument("--mode", default="factor", choices=["factor", "legacy"])

    decision_subs.add_parser("info", help="Show decision configuration")
    decision_subs.add_parser("list-models", help="List available models")

    # Low-frequency factor-genome evolution.  The job only ever produces
    # candidates that an administrator must approve.
    evolution = subparsers.add_parser(
        "strategy-evolution", help="Factor genome evolution commands"
    )
    evolution_subs = evolution.add_subparsers(dest="evolution_command", required=True)
    evolution_subs.add_parser("run", help="Run one weekly evolution pass now")
    evolution_subs.add_parser("status", help="Show the active factor genome version")

    args = parser.parse_args()

    if args.command == "api":
        uvicorn.run("ashare_ai.api.app:app", host=args.host, port=args.port)
    elif args.command == "migrate":
        migrate_database()
    elif args.command == "doctor":
        checks = run_doctor(check_market=not args.skip_market)
        print(format_doctor(checks))
        if any(check.level == "FAIL" for check in checks):
            raise SystemExit(1)
    elif args.command == "decision":
        _run_decision_command(args)
    elif args.command == "strategy-evolution":
        _run_evolution_command(args)


def _run_decision_command(args: argparse.Namespace) -> None:
    """Run decision commands"""
    import asyncio
    from pathlib import Path

    from ashare_ai.cli_commands import decision_commands

    if args.decision_command == "predict":
        asyncio.run(decision_commands.predict_single(
            symbol=args.symbol,
            bundle_dir=Path(args.bundle_dir) if args.bundle_dir else None,
            mode=args.mode,
        ))
    elif args.decision_command == "batch":
        asyncio.run(decision_commands.predict_batch(
            symbols_file=Path(args.symbols_file),
            output_file=Path(args.output_file),
            bundle_dir=Path(args.bundle_dir) if args.bundle_dir else None,
            mode=args.mode,
        ))
    elif args.decision_command == "info":
        decision_commands.show_info()
    elif args.decision_command == "list-models":
        decision_commands.list_models()


def _run_evolution_command(args: argparse.Namespace) -> None:
    """Run factor-genome evolution commands"""
    from ashare_ai.cli_commands import strategy_evolution_commands

    if args.evolution_command == "run":
        strategy_evolution_commands.run_once()
    elif args.evolution_command == "status":
        strategy_evolution_commands.show_status()


if __name__ == "__main__":
    main()
