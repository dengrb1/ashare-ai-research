from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ashare_ai.core.config import get_settings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.orchestration.runner import seconds_until_next_tick

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueSpec:
    kind: str
    pending: str
    processing: str


QUEUE_SPECS = (
    QueueSpec("exit-review", "ashare:exit-advice:pending", "ashare:exit-advice:processing"),
    QueueSpec("research", "ashare:research:pending", "ashare:research:processing"),
    QueueSpec("trade-plan", "ashare:trade-plan:pending", "ashare:trade-plan:processing"),
    QueueSpec("backtest", "ashare:backtest:pending", "ashare:backtest:processing"),
)


def execute_isolated(kind: str, job_id: str) -> int:
    """Run one heavy job in a child so its scientific heap is returned to the OS."""
    completed = subprocess.run(
        [sys.executable, "-m", "ashare_ai.orchestration.run_job", kind, job_id],
        check=False,
    )
    return completed.returncode


def build_queues(client: Any) -> list[tuple[QueueSpec, RedisLeasedQueue]]:
    lease_seconds = get_settings().worker_lease_seconds
    return [
        (
            spec,
            RedisLeasedQueue(
                client,
                pending=spec.pending,
                processing=spec.processing,
                lease_seconds=lease_seconds,
            ),
        )
        for spec in QUEUE_SPECS
    ]


def run_loop(*, max_iterations: int | None = None) -> None:
    import redis

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    queues = build_queues(client)
    next_schedule_check = 0.0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        now_monotonic = time.monotonic()
        if now_monotonic >= next_schedule_check:
            return_code = execute_isolated("schedule", "tick")
            if return_code:
                logger.error(
                    "isolated automatic research dispatch exited with status %s",
                    return_code,
                )
            next_schedule_check = now_monotonic + seconds_until_next_tick(datetime.now(SHANGHAI))

        claimed = False
        for spec, queue in queues:
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
            break
        iterations += 1
        if not claimed and (max_iterations is None or iterations < max_iterations):
            time.sleep(1)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
