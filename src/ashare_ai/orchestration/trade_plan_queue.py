from __future__ import annotations

from ashare_ai.core.config import get_settings
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue

QUEUE_NAME = "ashare:trade-plan:pending"
PROCESSING_QUEUE_NAME = "ashare:trade-plan:processing"
PROMPT_VERSION = "trade-plan-explanation-v2"


def enqueue_trade_plan(plan_id: str, redis_url: str | None = None) -> None:
    """Queue a plan without importing the heavy optimization and Parquet runtime."""

    import redis

    client = redis.Redis.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    ).enqueue(plan_id)
