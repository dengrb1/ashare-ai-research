from __future__ import annotations

import argparse

import uvicorn

from ashare_ai.doctor import format_doctor, run_doctor


def main() -> None:
    parser = argparse.ArgumentParser(prog="ashare-ai")
    subparsers = parser.add_subparsers(dest="command", required=True)
    api = subparsers.add_parser("api", help="Run the FastAPI service")
    api.add_argument("--host", default="0.0.0.0")
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
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
    elif args.command == "doctor":
        checks = run_doctor(check_market=not args.skip_market)
        print(format_doctor(checks))
        if any(check.level == "FAIL" for check in checks):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
