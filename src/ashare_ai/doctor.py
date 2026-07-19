from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Literal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.security import safe_error_message

Level = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    level: Level
    message: str


def _result(name: str, function: Callable[[], str], *, warning: bool = False) -> DoctorCheck:
    try:
        return DoctorCheck(name, "PASS", function())
    except Exception as exc:
        return DoctorCheck(
            name,
            "WARN" if warning else "FAIL",
            f"{type(exc).__name__}: {safe_error_message(exc)}",
        )


def _production_security(settings: Settings) -> str:
    settings.validate_production_security()
    return "production security invariants are satisfied"


def _factory(path: str | None) -> str:
    if not path or ":" not in path:
        raise RuntimeError("factory is not configured as package.module:callable")
    module_name, attribute = path.rsplit(":", 1)
    value = getattr(import_module(module_name), attribute)
    if not callable(value):
        raise TypeError("configured attribute is not callable")
    return f"{module_name}:{attribute} is importable"


def _policy(settings: Settings) -> str:
    path = settings.policy_config_path
    if not path.is_file():
        raise FileNotFoundError(f"policy file not found: {path}")
    return f"policy file is readable: {path}"


def _dependency_lock(settings: Settings) -> str:
    path = settings.dependency_lock_path
    if not path.is_file():
        raise FileNotFoundError(f"dependency lock not found: {path}")
    return f"dependency lock is readable: {path}"


def _database(settings: Settings) -> str:
    url = make_url(settings.database_url)
    database = url.database
    if url.get_backend_name() == "sqlite" and database not in {None, "", ":memory:"}:
        assert database is not None
        path = Path(database)
        if not path.is_file():
            raise FileNotFoundError(f"SQLite database does not exist: {path}")
    connect_args = {"connect_timeout": 3} if url.get_backend_name() == "postgresql" else {}
    engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    return f"database is reachable ({url.get_backend_name()})"


def _redis(settings: Settings) -> str:
    import redis

    client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    if client.ping() is not True:
        raise RuntimeError("PING did not return true")
    return "Redis is reachable"


def _object_store(settings: Settings) -> str:
    if settings.object_store_endpoint:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.object_store_endpoint,
            aws_access_key_id=settings.object_store_access_key,
            aws_secret_access_key=settings.object_store_secret_key,
            use_ssl=settings.object_store_secure,
            config=Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 1}),
        )
        client.head_bucket(Bucket=settings.object_store_bucket)
        return "object-store endpoint and bucket are reachable"
    root = settings.lake_root.parent / "objects"
    if not root.is_dir():
        raise FileNotFoundError(f"local object directory does not exist: {root}")
    return f"local object directory is readable: {root}"


def _worker_modules() -> str:
    for module_name in (
        "ashare_ai.orchestration.research_worker",
        "ashare_ai.orchestration.backtest_worker",
        "ashare_ai.orchestration.serial_worker",
        "ashare_ai.orchestration.redis_queue",
    ):
        import_module(module_name)
    return "research/backtest worker modules are importable"


def _market() -> str:
    from ashare_ai.market.service import get_market_data_service

    rows = get_market_data_service().quotes(["000001.SZ"])
    if not rows:
        raise RuntimeError("market provider returned no quote")
    source = rows[0].get("status", {}).get("source", "unknown")
    return f"market quote path is reachable (source={source})"


def run_doctor(*, settings: Settings | None = None, check_market: bool = True) -> list[DoctorCheck]:
    effective = settings or get_settings()
    pipeline_factory = (
        os.environ.get("ASHARE_PIPELINE_FACTORY") or effective.ashare_pipeline_factory
    )
    stage_factory = (
        os.environ.get("ASHARE_STAGE_BACKEND_FACTORY")
        or effective.ashare_stage_backend_factory
    )
    backtest_factory = (
        os.environ.get("ASHARE_BACKTEST_EXECUTOR_FACTORY")
        or effective.ashare_backtest_executor_factory
    )
    production_check = (
        _result("production-security", lambda: _production_security(effective))
        if effective.app_env.casefold() == "production"
        else DoctorCheck(
            "production-security",
            "WARN",
            "APP_ENV is not production; public-deployment checks are inactive",
        )
    )
    checks = [
        production_check,
        _result("policy-config", lambda: _policy(effective)),
        _result("dependency-lock", lambda: _dependency_lock(effective)),
        _result("pipeline-factory", lambda: _factory(pipeline_factory)),
        _result("stage-backend-factory", lambda: _factory(stage_factory)),
        _result("backtest-executor-factory", lambda: _factory(backtest_factory)),
        _result("worker-modules", _worker_modules),
        _result("database", lambda: _database(effective)),
        _result("redis", lambda: _redis(effective)),
        _result("object-store", lambda: _object_store(effective)),
    ]
    if check_market:
        checks.append(_result("market-connectivity", _market))
    else:
        checks.append(DoctorCheck("market-connectivity", "WARN", "skipped by --skip-market"))
    return checks


def format_doctor(checks: list[DoctorCheck]) -> str:
    return "\n".join(f"[{check.level}] {check.name}: {check.message}" for check in checks)
