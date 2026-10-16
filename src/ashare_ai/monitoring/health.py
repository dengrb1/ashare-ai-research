"""Bounded runtime dependency checks shared by health and monitor jobs."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.observability.runtime_resources import sample_runtime_resources
from ashare_ai.orchestration.worker_status import read_heartbeats
from ashare_ai.storage.database import SessionLocal


def check_runtime_dependencies(
    settings: Settings | None = None,
    *,
    include_external: bool = False,
) -> dict[str, Any]:
    """Return sanitized DB/Redis/worker/resource state with safe degradation."""

    settings = settings or get_settings()
    result: dict[str, Any] = {"checked_at": datetime.now(UTC).isoformat()}
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        result["database"] = {"status": "ok"}
    except Exception as exc:  # pragma: no cover - driver-specific failures
        result["database"] = {"status": "unavailable", "error": type(exc).__name__}

    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        heartbeats = read_heartbeats(client)
        result["redis"] = {"status": "ok", "workers": len(heartbeats)}
        result["workers"] = _worker_status(heartbeats)
        result["resources"] = sample_runtime_resources(heartbeats)
        with suppress(Exception):
            client.close()
    except Exception as exc:  # pragma: no cover - optional Redis deployment
        result["redis"] = {"status": "unavailable", "error": type(exc).__name__}
        result["workers"] = {"status": "unknown", "items": []}
        # Resource sampling is independent from Redis so an outage does not
        # hide CPU, memory, and disk pressure from the operator.
        result["resources"] = sample_runtime_resources([])

    if include_external:
        from ashare_ai.core.health import check_infrastructure_health

        result["external"] = check_infrastructure_health(settings)
    return result


def _worker_status(rows: list[dict[str, object]]) -> dict[str, Any]:
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for row in rows:
        raw = row.get("last_heartbeat_at")
        age = None
        if isinstance(raw, str):
            with suppress(ValueError):
                age = max(0.0, (now - datetime.fromisoformat(raw.replace("Z", "+00:00"))).total_seconds())
        healthy = bool(row.get("healthy")) and (age is None or age <= 120)
        items.append({"worker_id": row.get("worker_id"), "role": row.get("role"), "healthy": healthy, "age_seconds": age})
    return {"status": "ok" if items and all(item["healthy"] for item in items) else "degraded", "items": items}


def public_health_payload(settings: Settings | None = None) -> dict[str, Any]:
    """Build the compact public health additions without exposing URLs or secrets."""

    details = check_runtime_dependencies(settings, include_external=False)
    return {
        "redis": details.get("redis"),
        "workers": details.get("workers"),
        "resources": {
            "level": (details.get("resources") or {}).get("level", "UNKNOWN"),
            "warnings": (details.get("resources") or {}).get("warnings", []),
        },
    }
