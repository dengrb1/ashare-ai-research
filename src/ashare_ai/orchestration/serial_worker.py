from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ashare_ai.core import energy_saving
from ashare_ai.core.config import get_settings
from ashare_ai.core.energy_saving import DEEP_STANDBY_SECONDS, EVALUATION_INTERVAL_SECONDS
from ashare_ai.core.system_settings import SystemConfigurationService, SystemRuntimeSettings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.market.warmup import warm_market_if_due
from ashare_ai.observability.memory_reclaimer import reclaim_runtime_memory
from ashare_ai.orchestration.isolated_job import execute_isolated
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.orchestration.runner import seconds_until_next_tick
from ashare_ai.orchestration.worker_status import publish_heartbeat
from ashare_ai.storage.database import SessionLocal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueSpec:
    kind: str
    pending: str
    processing: str
    delayed: str | None = None


QUEUE_SPECS = (
    QueueSpec(
        "personal-archive",
        "ashare:personal-archive:pending",
        "ashare:personal-archive:processing",
    ),
    QueueSpec(
        "research",
        "ashare:research:pending",
        "ashare:research:processing",
        "ashare:research:delayed",
    ),
    QueueSpec("trade-plan", "ashare:trade-plan:pending", "ashare:trade-plan:processing"),
    QueueSpec("backtest", "ashare:backtest:pending", "ashare:backtest:processing"),
)


def build_queues(
    client: Any,
    *,
    execution_mode: str | None = None,
    lease_seconds: int | None = None,
) -> list[tuple[QueueSpec, RedisLeasedQueue]]:
    settings = get_settings()
    mode = execution_mode or getattr(settings, "research_execution_mode", "SERIAL")
    active_specs = tuple(spec for spec in QUEUE_SPECS if mode != "DUAL" or spec.kind != "research")
    effective_lease = lease_seconds or settings.worker_lease_seconds
    return [
        (
            spec,
            RedisLeasedQueue(
                client,
                pending=spec.pending,
                processing=spec.processing,
                delayed=spec.delayed,
                lease_seconds=effective_lease,
            ),
        )
        for spec in active_specs
    ]


def _load_worker_runtime() -> SystemRuntimeSettings | None:
    try:
        with SessionLocal() as session:
            return SystemConfigurationService().resolve(session)
    except Exception:
        # Before migrations have completed a worker must not invent an
        # override.  It uses the environment's safe SERIAL default instead.
        logger.exception("could not load persisted worker topology; using environment baseline")
        return None


def _energy_saving_standby(client: Any) -> int:
    """Return deep-standby seconds when energy saving is active, else 0.

    Settings are resolved fresh on every evaluation so an administrator can
    enable the mode without restarting the worker.  Any evaluation failure
    keeps the worker awake: an energy-saving bug must never block queue work.
    """
    try:
        with SessionLocal() as session:
            settings = SystemConfigurationService().resolve(session).settings
            state = energy_saving.evaluate(
                redis_client=client, session=session, settings=settings
            )
        return state.deep_standby_seconds
    except Exception:
        logger.exception("energy-saving evaluation failed; worker stays awake")
        return 0


def run_loop(*, max_iterations: int | None = None) -> None:
    import redis

    runtime = _load_worker_runtime()
    settings = runtime.settings if runtime is not None else get_settings()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    queues = (
        build_queues(
            client,
            execution_mode=runtime.execution_mode,
            lease_seconds=runtime.settings.worker_lease_seconds,
        )
        if runtime is not None
        else build_queues(client)
    )
    next_schedule_check = 0.0
    next_maintenance = 0.0
    next_energy_check = 0.0
    energy_standby = False
    previous_energy_standby = False
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        now_monotonic = time.monotonic()
        if runtime is not None:
            publish_heartbeat(
                client, role="job-worker", runtime=runtime, energy_saving=energy_standby
            )
        if now_monotonic >= next_energy_check:
            energy_standby = bool(_energy_saving_standby(client))
            next_energy_check = now_monotonic + EVALUATION_INTERVAL_SECONDS
            if energy_standby and not previous_energy_standby:
                reclaim_runtime_memory(
                    settings, reason="job-worker-energy-standby", force=True
                )
            previous_energy_standby = energy_standby
        if now_monotonic >= next_maintenance:
            return_code = execute_isolated("maintenance", "tick")
            if return_code:
                logger.error("isolated maintenance exited with status %s", return_code)
            next_maintenance = now_monotonic + 300
        if now_monotonic >= next_schedule_check:
            return_code = execute_isolated("schedule", "tick")
            if return_code:
                logger.error(
                    "isolated automatic research dispatch exited with status %s",
                    return_code,
                )
            next_schedule_check = now_monotonic + seconds_until_next_tick(datetime.now(SHANGHAI))
        # Warm the shared market cache (quotes + daily klines for the union of
        # watchlists/positions) during trading hours.  The gate is cheap when
        # disabled/after close and the actual fetch runs in a background thread,
        # so this never delays queue polling or other scheduled work.
        warm_market_if_due()
        if energy_standby:
            # Deep standby: keep the scheduler and maintenance alive but stop
            # per-second queue polling.  A new job flips the state on the next
            # evaluation, so re-enable latency is at most one standby wake-up.
            iterations += 1
            if max_iterations is None or iterations < max_iterations:
                time.sleep(DEEP_STANDBY_SECONDS)
            continue

        claimed = False
        for spec, queue in queues:
            queue.promote_due()
            queue.requeue_expired()
            job_id = queue.claim()
            if job_id is None:
                continue
            claimed = True
            try:
                with queue.heartbeat(job_id):
                    return_code = execute_isolated(spec.kind, job_id)
                    if return_code:
                        logger.error(
                            "isolated %s job %s exited with status %s",
                            spec.kind,
                            job_id,
                            return_code,
                        )
            finally:
                queue.acknowledge(job_id)
                reclaim_runtime_memory(
                    settings, reason=f"job-worker-{spec.kind}-complete"
                )
            break
        iterations += 1
        if not claimed and (max_iterations is None or iterations < max_iterations):
            time.sleep(1)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
