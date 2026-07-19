from __future__ import annotations

import argparse

import uvicorn
from sqlalchemy import create_engine, inspect

from ashare_ai.core.config import get_settings
from ashare_ai.doctor import format_doctor, run_doctor
from ashare_ai.storage.models import Base


def migrate_database() -> str:
    """Bootstrap an empty database at head; migrate populated databases revision by revision."""

    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
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


if __name__ == "__main__":
    main()
