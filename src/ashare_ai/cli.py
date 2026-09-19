from __future__ import annotations

import argparse

import uvicorn
from sqlalchemy import create_engine, inspect

from ashare_ai.core.config import get_settings, runtime_resource_path
from ashare_ai.doctor import format_doctor, run_doctor
from ashare_ai.storage.models import Base


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

    # Decision commands integration
    decision = subparsers.add_parser("decision", help="Decision mode commands")
    decision_subs = decision.add_subparsers(dest="decision_command", required=True)

    predict_cmd = decision_subs.add_parser("predict", help="Single prediction")
    predict_cmd.add_argument("symbol", help="Stock symbol")
    predict_cmd.add_argument("--bundle-dir", type=str, help="Bundle directory")
    predict_cmd.add_argument("--mode", default="legacy", choices=["legacy", "jev"])

    batch_cmd = decision_subs.add_parser("batch", help="Batch prediction")
    batch_cmd.add_argument("symbols_file", help="File with symbols")
    batch_cmd.add_argument("output_file", help="Output JSON file")
    batch_cmd.add_argument("--bundle-dir", type=str, help="Bundle directory")
    batch_cmd.add_argument("--mode", default="legacy", choices=["legacy", "jev"])

    decision_subs.add_parser("info", help="Show decision configuration")
    decision_subs.add_parser("list-models", help="List available models")

    # Training commands integration
    train = subparsers.add_parser("train-jev", help="Jev model training commands")
    train_subs = train.add_subparsers(dest="train_command", required=True)

    gen_dataset = train_subs.add_parser("generate-dataset", help="Generate training dataset")
    gen_dataset.add_argument("--bundle-dir", required=True, help="Bundle directory")
    gen_dataset.add_argument("--output-dir", required=True, help="Output directory")
    gen_dataset.add_argument("--start-date", type=str, help="Start date (YYYY-MM-DD)")
    gen_dataset.add_argument("--end-date", type=str, help="End date (YYYY-MM-DD)")

    train_cmd = train_subs.add_parser("train", help="Train Jev model")
    train_cmd.add_argument("--dataset-dir", required=True, help="Dataset directory")
    train_cmd.add_argument("--checkpoint-dir", required=True, help="Checkpoint directory")
    train_cmd.add_argument("--epochs", type=int, default=100, help="Number of epochs")
    train_cmd.add_argument("--batch-size", type=int, default=256, help="Batch size")

    eval_cmd = train_subs.add_parser("evaluate", help="Evaluate model")
    eval_cmd.add_argument("--checkpoint", required=True, help="Checkpoint path")
    eval_cmd.add_argument("--dataset-dir", required=True, help="Test dataset directory")

    # Backtest comparison commands
    backtest = subparsers.add_parser("backtest-compare", help="Backtest comparison commands")
    backtest_subs = backtest.add_subparsers(dest="backtest_command", required=True)

    compare_cmd = backtest_subs.add_parser("compare", help="Compare Legacy vs Jev")
    compare_cmd.add_argument("--bundle-dir", required=True, help="Bundle directory")
    compare_cmd.add_argument("--start-date", required=True, help="Start date")
    compare_cmd.add_argument("--end-date", required=True, help="End date")
    compare_cmd.add_argument("--output", help="Output report path")

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
    elif args.command == "train-jev":
        _run_train_command(args)
    elif args.command == "backtest-compare":
        _run_backtest_command(args)


def _run_decision_command(args: argparse.Namespace) -> None:
    """Run decision commands"""
    import asyncio
    from pathlib import Path

    from ashare_ai.cli import decision_commands

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


def _run_train_command(args: argparse.Namespace) -> None:
    """Run training commands"""
    import asyncio
    from datetime import date
    from pathlib import Path

    from ashare_ai.cli import train_jev_commands

    if args.train_command == "generate-dataset":
        start_date = date.fromisoformat(args.start_date) if args.start_date else None
        end_date = date.fromisoformat(args.end_date) if args.end_date else None
        asyncio.run(train_jev_commands.generate_dataset(
            bundle_dir=Path(args.bundle_dir),
            output_dir=Path(args.output_dir),
            start_date=start_date,
            end_date=end_date,
        ))
    elif args.train_command == "train":
        asyncio.run(train_jev_commands.train_model(
            dataset_dir=Path(args.dataset_dir),
            checkpoint_dir=Path(args.checkpoint_dir),
            num_epochs=args.epochs,
            batch_size=args.batch_size,
        ))
    elif args.train_command == "evaluate":
        asyncio.run(train_jev_commands.evaluate_model(
            checkpoint_path=Path(args.checkpoint),
            dataset_dir=Path(args.dataset_dir),
        ))


def _run_backtest_command(args: argparse.Namespace) -> None:
    """Run backtest comparison commands"""
    import asyncio
    from datetime import date
    from pathlib import Path

    from ashare_ai.cli import backtest_compare_commands

    if args.backtest_command == "compare":
        asyncio.run(backtest_compare_commands.compare_modes(
            bundle_dir=Path(args.bundle_dir),
            start_date=date.fromisoformat(args.start_date),
            end_date=date.fromisoformat(args.end_date),
            output_path=Path(args.output) if args.output else None,
        ))


if __name__ == "__main__":
    main()
