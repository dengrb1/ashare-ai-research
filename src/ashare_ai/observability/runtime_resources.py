"""Best-effort, operator-facing runtime resource sampling."""

from __future__ import annotations

import os
import statistics
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import psutil

MIB = 1024**2
DUAL_WORKER_COUNT = 2
DEFAULT_WORKER_BASELINE_BYTES = 192 * MIB
DEFAULT_WORKER_LIMIT_BYTES = 700 * MIB
PROJECTED_HEADROOM_BYTES = 512 * MIB

_PROCESS = psutil.Process()
_PROCESS_SAMPLE_LOCK = threading.Lock()


def _read_memory_value(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
        if not raw or raw == "max":
            return None
        value = int(raw)
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def cgroup_memory(
    roots: tuple[Path, ...] = (Path("/sys/fs/cgroup"),),
) -> tuple[int | None, int | None]:
    """Return current and limited cgroup memory for v2 or v1."""

    candidates = (
        ("memory.current", "memory.max"),
        ("memory/memory.usage_in_bytes", "memory/memory.limit_in_bytes"),
    )
    for root in roots:
        for usage_name, limit_name in candidates:
            usage = _read_memory_value(root / usage_name)
            limit = _read_memory_value(root / limit_name)
            if usage is None and limit is None:
                continue
            # v1 represents an unlimited cgroup with an enormous sentinel.
            if limit is not None and limit >= 1 << 60:
                limit = None
            return usage, limit
    return None, None


def sample_current_service(*, service_id: str, role: str) -> dict[str, object]:
    memory_used, memory_limit = cgroup_memory()
    if memory_used is None:
        try:
            memory_used = int(_PROCESS.memory_info().rss)
        except (OSError, psutil.Error):
            memory_used = None
    try:
        with _PROCESS_SAMPLE_LOCK:
            cpu_percent = round(float(_PROCESS.cpu_percent(interval=None)), 1)
    except (OSError, psutil.Error):
        cpu_percent = None
    return {
        "service_id": service_id,
        "role": role,
        "healthy": True,
        "memory_used_bytes": memory_used,
        "memory_limit_bytes": memory_limit,
        "cpu_percent": cpu_percent,
        "collected_at": datetime.now(UTC).isoformat(),
    }


def _worker_measurements(
    workers: list[dict[str, object]], role: str, field: str
) -> list[int]:
    values: list[int] = []
    for worker in workers:
        value = worker.get(field)
        if (
            worker.get("healthy")
            and worker.get("role") == role
            and isinstance(value, int)
            and value > 0
        ):
            values.append(value)
    return values


def _dual_estimate(
    workers: list[dict[str, object]], *, available_bytes: int, memory_percent: float
) -> dict[str, object]:
    observed = _worker_measurements(workers, "research-worker", "memory_used_bytes")
    source = "research-worker"
    if not observed:
        observed = _worker_measurements(workers, "job-worker", "memory_used_bytes")
        source = "job-worker"
    if observed:
        per_worker_typical = int(statistics.median(observed))
    else:
        per_worker_typical = DEFAULT_WORKER_BASELINE_BYTES
        source = "fallback"

    limits = _worker_measurements(workers, "research-worker", "memory_limit_bytes")
    if not limits:
        limits = _worker_measurements(workers, "job-worker", "memory_limit_bytes")
    per_worker_ceiling = max(limits) if limits else DEFAULT_WORKER_LIMIT_BYTES
    typical_increment = DUAL_WORKER_COUNT * per_worker_typical
    ceiling_increment = DUAL_WORKER_COUNT * per_worker_ceiling
    projected_available = available_bytes - typical_increment

    level: Literal["NORMAL", "WARNING", "CRITICAL"] = "NORMAL"
    messages: list[str] = []
    if memory_percent >= 90 or projected_available <= 0:
        level = "CRITICAL"
    elif (
        memory_percent >= 80
        or available_bytes < ceiling_increment
        or projected_available < PROJECTED_HEADROOM_BYTES
    ):
        level = "WARNING"
    if memory_percent >= 80:
        messages.append("MEMORY_USAGE_HIGH")
    if available_bytes < ceiling_increment:
        messages.append("DUAL_CEILING_EXCEEDS_AVAILABLE")
    if projected_available < PROJECTED_HEADROOM_BYTES:
        messages.append("DUAL_PROJECTED_HEADROOM_LOW")
    return {
        "worker_replicas": DUAL_WORKER_COUNT,
        "estimate_source": source,
        "typical_per_worker_bytes": per_worker_typical,
        "typical_increment_bytes": typical_increment,
        "maximum_increment_bytes": ceiling_increment,
        "projected_available_bytes": projected_available,
        "level": level,
        "messages": messages,
    }


def sample_runtime_resources(workers: list[dict[str, object]]) -> dict[str, Any]:
    """Return a sanitized snapshot; filesystem paths never leave this module."""

    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    cpu_percent = round(float(psutil.cpu_percent(interval=None)), 1)
    memory_percent = round(float(memory.percent), 1)
    scope: Literal["HOST", "CONTAINER"] = (
        "CONTAINER" if Path("/.dockerenv").exists() or cgroup_memory()[1] else "HOST"
    )
    warnings: list[str] = []
    dual = _dual_estimate(
        workers, available_bytes=int(memory.available), memory_percent=memory_percent
    )
    level = str(dual["level"])
    if cpu_percent >= 90:
        warnings.append("CPU_USAGE_HIGH")
        if level == "NORMAL":
            level = "WARNING"
    if float(disk.percent) >= 85:
        warnings.append("DISK_USAGE_HIGH")
        if level == "NORMAL":
            level = "WARNING"
    warnings = [*cast(list[str], dual["messages"]), *warnings]

    api_service = sample_current_service(service_id="api", role="api")
    services = [api_service]
    for worker in workers:
        services.append(
            {
                "service_id": worker.get("worker_id"),
                "role": worker.get("role"),
                "healthy": bool(worker.get("healthy")),
                "memory_used_bytes": worker.get("memory_used_bytes"),
                "memory_limit_bytes": worker.get("memory_limit_bytes"),
                "cpu_percent": worker.get("cpu_percent"),
                "collected_at": worker.get("last_heartbeat_at"),
            }
        )
    return {
        "collected_at": datetime.now(UTC),
        "scope": scope,
        "scope_label": (
            "Docker VM 总体；Python 服务占用来自各自 cgroup"
            if scope == "CONTAINER"
            else "当前服务器运行环境"
        ),
        "memory": {
            "total_bytes": int(memory.total),
            "used_bytes": int(memory.used),
            "available_bytes": int(memory.available),
            "percent": memory_percent,
        },
        "cpu": {
            "percent": cpu_percent,
            "logical_cores": int(psutil.cpu_count(logical=True) or 1),
        },
        "disk": {
            "total_bytes": int(disk.total),
            "used_bytes": int(disk.used),
            "available_bytes": int(disk.free),
            "percent": round(float(disk.percent), 1),
        },
        "services": services,
        "topology_estimate": dual,
        "level": level,
        "warnings": warnings,
    }
