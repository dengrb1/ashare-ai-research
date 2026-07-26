"""Small Redis heartbeats used only for operator-facing worker status."""

from __future__ import annotations

import json
import os
import socket
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from ashare_ai.core.system_settings import SystemRuntimeSettings
from ashare_ai.observability.runtime_resources import sample_current_service

HEARTBEAT_TTL_SECONDS = 90
_PREFIX = "ashare:workers:"


def worker_id(role: str) -> str:
    host = os.environ.get("HOSTNAME") or socket.gethostname() or "unknown"
    return f"{role}:{host}:{os.getpid()}"


def publish_heartbeat(client: Any, *, role: str, runtime: SystemRuntimeSettings) -> str:
    identifier = worker_id(role)
    resources = sample_current_service(service_id=identifier, role=role)
    payload = {
        "worker_id": identifier,
        "role": role,
        "healthy": True,
        "loaded_mode": runtime.execution_mode,
        "topology_sha256": runtime.topology_sha256,
        "config_sha256": runtime.config_sha256,
        "last_heartbeat_at": datetime.now(UTC).isoformat(),
        "memory_used_bytes": resources["memory_used_bytes"],
        "memory_limit_bytes": resources["memory_limit_bytes"],
        "cpu_percent": resources["cpu_percent"],
    }
    # Queue test doubles and a temporary Redis outage must not turn an
    # otherwise safe worker into a consumer with unknown behaviour.
    with suppress(AttributeError, OSError):
        client.set(f"{_PREFIX}{identifier}", json.dumps(payload), ex=HEARTBEAT_TTL_SECONDS)
    return identifier


def read_heartbeats(client: Any) -> list[dict[str, object]]:
    try:
        keys = list(client.scan_iter(match=f"{_PREFIX}*"))
        raw_values = client.mget(keys) if keys else []
    except (AttributeError, OSError):
        return []
    rows: list[dict[str, object]] = []
    for raw in raw_values:
        if not isinstance(raw, (str, bytes)):
            continue
        try:
            parsed = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("worker_id"), str):
            rows.append(parsed)
    return sorted(rows, key=lambda item: str(item["worker_id"]))
