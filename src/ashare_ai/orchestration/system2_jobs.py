"""Durable System-2 diagnostic jobs for low-confidence decisions.

The payload is a frozen :class:`MarketState`; the LLM may explain risk and
uncertainty but cannot create an order or mutate a research snapshot.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ashare_ai.agents.decision.models import MarketState
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue

QUEUE_NAME = "ashare:system2-diagnostic:pending"
PROCESSING_QUEUE_NAME = "ashare:system2-diagnostic:processing"
PAYLOAD_PREFIX = "ashare:system2-diagnostic:payload:"
STATUS_PREFIX = "ashare:system2-diagnostic:status:"
RETENTION_SECONDS = 24 * 60 * 60


def _keys(job_id: str) -> tuple[str, str]:
    return f"{PAYLOAD_PREFIX}{job_id}", f"{STATUS_PREFIX}{job_id}"


def _queue(client: Any) -> RedisLeasedQueue:
    return RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )


def enqueue_system2_diagnostic(
    market_state: MarketState,
    reason: str,
    redis_url: str | None = None,
) -> str:
    """Persist and enqueue one idempotent diagnostic request."""

    import redis

    payload = {
        "market_state": market_state.model_dump(mode="json"),
        "reason": reason,
    }
    job_id = stable_hash({"kind": "system2-diagnostic-v1", **payload})
    payload_key, status_key = _keys(job_id)
    client = redis.Redis.from_url(redis_url or get_settings().redis_url, decode_responses=True)
    if client.exists(status_key):
        return job_id
    now = datetime.now(UTC).isoformat()
    client.hset(payload_key, mapping={"payload": json.dumps(payload, ensure_ascii=False)})
    client.expire(payload_key, RETENTION_SECONDS)
    client.hset(status_key, mapping={"status": "QUEUED", "created_at": now, "reason": reason})
    client.expire(status_key, RETENTION_SECONDS)
    _queue(client).enqueue(job_id)
    return job_id


def _set_status(client: Any, job_id: str, **values: object) -> None:
    _, status_key = _keys(job_id)
    client.hset(status_key, mapping={key: str(value) for key, value in values.items()})
    client.expire(status_key, RETENTION_SECONDS)


def get_system2_status(client: Any, job_id: str) -> dict[str, str] | None:
    _, status_key = _keys(job_id)
    values = client.hgetall(status_key)
    return dict(values) if values else None


def run_system2_diagnostic_job(job_id: str) -> None:
    """Run one queued diagnostic in the isolated worker process."""

    import redis

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    payload_key, _ = _keys(job_id)
    raw = client.hget(payload_key, "payload")
    if not raw:
        _set_status(client, job_id, status="FAILED", error="diagnostic payload is missing")
        return
    started_at = datetime.now(UTC)
    _set_status(client, job_id, status="RUNNING", started_at=started_at.isoformat())
    try:
        payload = json.loads(raw)
        market_state = MarketState.model_validate(payload["market_state"])
        reason = str(payload.get("reason") or "UNKNOWN")
        from ashare_ai.agents.decision.router import _llm_system2_dispatch

        result = asyncio.run(_llm_system2_dispatch(market_state, reason))
        _set_status(
            client,
            job_id,
            status="SUCCEEDED",
            completed_at=datetime.now(UTC).isoformat(),
            summary=result.summary,
            risk_flags=json.dumps(result.risk_flags, ensure_ascii=False),
            confidence=result.confidence,
        )
    except Exception as exc:
        _set_status(
            client,
            job_id,
            status="FAILED",
            completed_at=datetime.now(UTC).isoformat(),
            error=type(exc).__name__,
        )
        raise
