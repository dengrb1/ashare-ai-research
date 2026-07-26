"""Lightweight exit-advice queue contract used by API and worker parents."""

from __future__ import annotations

from typing import Any

from ashare_ai.core.config import get_settings
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue

QUEUE_NAME = "ashare:exit-advice:pending"
PROCESSING_QUEUE_NAME = "ashare:exit-advice:processing"


def build_exit_advice_queue(client: Any) -> RedisLeasedQueue:
    return RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )


def enqueue_exit_advice(advice_id: str, redis_url: str | None = None) -> None:
    import redis

    client = redis.Redis.from_url(
        redis_url or get_settings().redis_url, decode_responses=True
    )
    build_exit_advice_queue(client).enqueue(advice_id)
