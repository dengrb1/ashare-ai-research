"""CLI commands for the low-frequency factor-genome evolution."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ashare_ai.core.system_settings import get_effective_settings
from ashare_ai.orchestration.strategy_evolution_jobs import (
    enqueue_strategy_evolution,
    get_strategy_evolution_status,
    weekly_job_id,
)
from ashare_ai.quant.factor_registry import FactorGenomeRegistry


def run_once() -> None:
    """Enqueue this week's evolution pass and report the job id."""

    config = get_effective_settings()
    if not config.strategy_evolution_enabled:
        print("strategy evolution is disabled in system settings")
        return
    job_id = enqueue_strategy_evolution(now=datetime.now(UTC))
    print(f"enqueued strategy evolution job {job_id}")


def show_status() -> None:
    """Print the active genome version and this week's job status."""

    import redis

    from ashare_ai.storage.database import SessionLocal

    with SessionLocal() as session:
        active = FactorGenomeRegistry(session).active_version()
    print(json.dumps(active, indent=2, ensure_ascii=False))

    client = redis.Redis.from_url(get_effective_settings().redis_url, decode_responses=True)
    status = get_strategy_evolution_status(client, weekly_job_id(datetime.now(UTC)))
    print(json.dumps(status or {"status": "NOT_QUEUED"}, indent=2, ensure_ascii=False))
