from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from redis.exceptions import ConnectionError as RedisConnectionError

from ashare_ai.observability import runtime_resources
from ashare_ai.observability.runtime_resources import (
    MIB,
    cgroup_memory,
    cgroup_memory_details,
    sample_current_service,
    sample_runtime_resources,
)
from ashare_ai.orchestration.worker_status import (
    publish_heartbeat,
    publish_service_heartbeat,
    read_heartbeats,
)


def test_cgroup_memory_reads_v2_and_v1(tmp_path: Path) -> None:
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "memory.current").write_text("123", encoding="ascii")
    (v2 / "memory.max").write_text("456", encoding="ascii")
    assert cgroup_memory((v2,)) == (123, 456)

    v1 = tmp_path / "v1" / "memory"
    v1.mkdir(parents=True)
    (v1 / "memory.usage_in_bytes").write_text("321", encoding="ascii")
    (v1 / "memory.limit_in_bytes").write_text("654", encoding="ascii")
    assert cgroup_memory((tmp_path / "v1",)) == (321, 654)


def test_cgroup_working_set_excludes_inactive_file(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "v2"
    root.mkdir()
    (root / "memory.current").write_text("1000", encoding="ascii")
    (root / "memory.max").write_text("2000", encoding="ascii")
    (root / "memory.stat").write_text(
        "anon 600\ninactive_file 250\nactive_file 100\n", encoding="ascii"
    )
    assert cgroup_memory_details((root,)) == (1000, 2000, 250)
    monkeypatch.setattr(
        runtime_resources, "cgroup_memory_details", lambda: (1000, 2000, 250)
    )
    service = sample_current_service(service_id="worker", role="job-worker")
    assert service["memory_used_bytes"] == 750
    assert service["memory_cache_bytes"] == 250


def test_runtime_snapshot_estimates_dual_from_job_worker(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_resources.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(
            total=8 * 1024**3,
            used=5 * 1024**3,
            available=3 * 1024**3,
            percent=62.5,
        ),
    )
    monkeypatch.setattr(
        runtime_resources.psutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            total=100 * 1024**3,
            used=50 * 1024**3,
            free=50 * 1024**3,
            percent=50.0,
        ),
    )
    monkeypatch.setattr(runtime_resources.psutil, "cpu_percent", lambda interval=None: 12.0)
    monkeypatch.setattr(runtime_resources.psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(
        runtime_resources,
        "sample_current_service",
        lambda **_kwargs: {
            "service_id": "api",
            "role": "api",
            "healthy": True,
            "memory_used_bytes": 100 * MIB,
            "memory_cache_bytes": 5 * MIB,
            "memory_limit_bytes": 320 * MIB,
            "cpu_percent": 1.0,
            "collected_at": "2026-07-26T00:00:00+00:00",
        },
    )
    workers = [
        {
            "worker_id": "job-worker:one:1",
            "role": "job-worker",
            "healthy": True,
            "memory_used_bytes": 200 * MIB,
            "memory_limit_bytes": 700 * MIB,
            "cpu_percent": 2.0,
            "last_heartbeat_at": "2026-07-26T00:00:00+00:00",
        }
    ]
    snapshot = sample_runtime_resources(workers)
    assert snapshot["topology_estimate"]["estimate_source"] == "job-worker"
    assert snapshot["topology_estimate"]["typical_increment_bytes"] == 400 * MIB
    assert snapshot["topology_estimate"]["maximum_increment_bytes"] == 1400 * MIB
    assert snapshot["level"] == "NORMAL"
    assert "path" not in json.dumps(snapshot, default=str).lower()


class _HeartbeatRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value

    def scan_iter(self, *, match: str):
        return iter(self.values)

    def mget(self, keys: list[str]) -> list[str]:
        return [self.values[key] for key in keys]


def test_worker_heartbeat_adds_resources_and_reads_legacy(monkeypatch) -> None:
    client = _HeartbeatRedis()
    runtime = SimpleNamespace(
        execution_mode="SERIAL", topology_sha256="a" * 64, config_sha256="b" * 64
    )
    monkeypatch.setattr(
        "ashare_ai.orchestration.worker_status.sample_current_service",
        lambda **_kwargs: {
            "memory_used_bytes": 10,
            "memory_limit_bytes": 20,
            "cpu_percent": 3.0,
        },
    )
    publish_heartbeat(client, role="job-worker", runtime=runtime)
    current = read_heartbeats(client)[0]
    assert current["memory_used_bytes"] == 10
    assert current["memory_limit_bytes"] == 20

    publish_service_heartbeat(client, role="exit-advice-worker")
    lightweight = next(
        row for row in read_heartbeats(client) if row["role"] == "exit-advice-worker"
    )
    assert lightweight["loaded_mode"] == "UNKNOWN"

    client.values["ashare:workers:legacy"] = json.dumps(
        {"worker_id": "legacy", "role": "job-worker", "healthy": True}
    )
    assert {row["worker_id"] for row in read_heartbeats(client)} == {
        "legacy",
        current["worker_id"],
        lightweight["worker_id"],
    }


def test_observability_redis_failure_does_not_escape(monkeypatch) -> None:
    class UnavailableRedis:
        def set(self, *_args: object, **_kwargs: object) -> None:
            raise RedisConnectionError("unavailable")

        def scan_iter(self, **_kwargs: object):
            raise RedisConnectionError("unavailable")

    monkeypatch.setattr(
        "ashare_ai.orchestration.worker_status.sample_current_service",
        lambda **_kwargs: {
            "memory_used_bytes": 10,
            "memory_limit_bytes": 20,
            "cpu_percent": 3.0,
        },
    )
    client = UnavailableRedis()
    assert publish_service_heartbeat(client, role="exit-advice-worker")
    assert read_heartbeats(client) == []
