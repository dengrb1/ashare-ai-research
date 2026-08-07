from __future__ import annotations

import logging
import time
from typing import Any

from ashare_ai.core import energy_saving
from ashare_ai.core.config import get_settings
from ashare_ai.core.energy_saving import DEEP_STANDBY_SECONDS, EVALUATION_INTERVAL_SECONDS
from ashare_ai.core.system_settings import SystemConfigurationService
from ashare_ai.observability.memory_reclaimer import reclaim_runtime_memory
from ashare_ai.orchestration.exit_advice_queue import build_exit_advice_queue
from ashare_ai.orchestration.isolated_job import execute_isolated
from ashare_ai.orchestration.worker_status import publish_service_heartbeat
from ashare_ai.storage.database import SessionLocal

logger = logging.getLogger(__name__)


def _energy_saving_standby(client: Any) -> int:
    """Return deep-standby seconds when energy saving is active, else 0.

    Settings are resolved fresh on every evaluation so an administrator can
    enable the mode without restarting the worker.  Any evaluation failure
    keeps the worker awake: an energy-saving bug must never block on-demand
    exit advice.
    """
    try:
        with SessionLocal() as session:
            settings = SystemConfigurationService().resolve(session).settings
            state = energy_saving.evaluate(
                redis_client=client, session=session, settings=settings
            )
        return state.deep_standby_seconds
    except Exception:
        logger.exception("energy-saving evaluation failed; exit worker stays awake")
        return 0


def run_loop(*, max_iterations: int | None = None) -> None:
    import redis

    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    queue = build_exit_advice_queue(client)
    next_energy_check = 0.0
    energy_standby = False
    previous_energy_standby = False
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        now_monotonic = time.monotonic()
        if now_monotonic >= next_energy_check:
            energy_standby = bool(_energy_saving_standby(client))
            next_energy_check = now_monotonic + EVALUATION_INTERVAL_SECONDS
            if energy_standby and not previous_energy_standby:
                reclaim_runtime_memory(
                    settings, reason="exit-worker-energy-standby", force=True
                )
            previous_energy_standby = energy_standby
        publish_service_heartbeat(
            client, role="exit-advice-worker", energy_saving=energy_standby
        )
        if energy_standby:
            # A new exit-advice enqueue flips the state on the next evaluation,
            # so on-demand re-enable latency is at most one standby wake-up.
            iterations += 1
            if max_iterations is None or iterations < max_iterations:
                time.sleep(DEEP_STANDBY_SECONDS)
            continue
        queue.requeue_expired()
        advice_id = queue.claim()
        if advice_id is None:
            iterations += 1
            if max_iterations is None or iterations < max_iterations:
                time.sleep(1)
            continue
        try:
            with queue.heartbeat(advice_id):
                return_code = execute_isolated("exit-review", advice_id)
                if return_code:
                    logger.error(
                        "isolated exit advice %s exited with status %s",
                        advice_id,
                        return_code,
                    )
        finally:
            queue.acknowledge(advice_id)
            reclaim_runtime_memory(settings, reason="exit-worker-job-complete")
        iterations += 1


if __name__ == "__main__":
    run_loop()
