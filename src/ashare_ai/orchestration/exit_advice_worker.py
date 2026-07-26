from __future__ import annotations

import logging
import time

from ashare_ai.core.config import get_settings
from ashare_ai.orchestration.exit_advice_queue import build_exit_advice_queue
from ashare_ai.orchestration.isolated_job import execute_isolated
from ashare_ai.orchestration.worker_status import publish_service_heartbeat

logger = logging.getLogger(__name__)


def run_loop(*, max_iterations: int | None = None) -> None:
    import redis

    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    queue = build_exit_advice_queue(client)
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        publish_service_heartbeat(client, role="exit-advice-worker")
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
        iterations += 1

if __name__ == "__main__":
    run_loop()
