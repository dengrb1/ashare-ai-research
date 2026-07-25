from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ashare_ai.agents.attachments import AttachmentService
from ashare_ai.core.config import get_settings
from ashare_ai.core.system_settings import SystemConfigurationService, SystemRuntimeSettings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.notifications.service import NotificationService
from ashare_ai.orchestration.personal_archive_jobs import cleanup_expired_archives
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


def execute_isolated(kind: str, job_id: str) -> int:
    """Run one heavy job in a child so its scientific heap is returned to the OS."""
    completed = subprocess.run(
        [sys.executable, "-m", "ashare_ai.orchestration.run_job", kind, job_id],
        check=False,
    )
    return completed.returncode


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
    next_attachment_cleanup = 0.0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        if runtime is not None:
            publish_heartbeat(client, role="job-worker", runtime=runtime)
        now_monotonic = time.monotonic()
        if now_monotonic >= next_attachment_cleanup:
            with SessionLocal() as session:
                deleted = AttachmentService(session).cleanup_expired()
            expired_archives = cleanup_expired_archives()
            with SessionLocal() as session:
                expired_notifications = NotificationService(session).cleanup_expired()
                if expired_notifications:
                    session.commit()
            if deleted:
                logger.info("purged %s expired chat attachments", deleted)
            if expired_archives:
                logger.info("purged %s expired personal archives", expired_archives)
            if expired_notifications:
                logger.info("purged %s expired notifications", expired_notifications)
            next_attachment_cleanup = now_monotonic + 300
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
            break
        iterations += 1
        if not claimed and (max_iterations is None or iterations < max_iterations):
            time.sleep(1)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
