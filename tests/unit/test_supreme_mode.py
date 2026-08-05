from __future__ import annotations

from ashare_ai.observability.runtime_resources import MIB
from ashare_ai.orchestration.supreme_mode import (
    RuntimeCapacity,
    SupremeModePolicy,
    resolve_execution_profile,
)


def _policy() -> SupremeModePolicy:
    return SupremeModePolicy(
        version="test-supreme-v1",
        max_data_fetch_workers=12,
        estimated_memory_per_fetch_worker_mib=48,
        reserve_memory_mib=160,
        cpu_warning_percent=75,
        cpu_critical_percent=90,
    )


def _capacity(*, cpu_percent: float, available_mib: int) -> RuntimeCapacity:
    return RuntimeCapacity(
        scope="CONTAINER",
        logical_cores=8,
        cpu_percent=cpu_percent,
        available_memory_bytes=available_mib * MIB,
        memory_limit_bytes=700 * MIB,
        active_memory_bytes=(700 - available_mib) * MIB,
    )


def test_supreme_mode_uses_bounded_data_parallelism_without_changing_model_concurrency() -> None:
    profile = resolve_execution_profile(
        supreme_mode=True,
        policy=_policy(),
        model_agent_max_concurrency=4,
        capacity=_capacity(cpu_percent=18, available_mib=1_024),
    )

    assert profile.mode == "SUPREME"
    assert profile.data_fetch_workers == 12
    assert profile.model_agent_max_concurrency == 4
    assert profile.manifest_value()["model_concurrency_changed"] is False
    assert profile.resource_level == "NORMAL"


def test_supreme_mode_throttles_for_cpu_and_memory_pressure() -> None:
    cpu_limited = resolve_execution_profile(
        supreme_mode=True,
        policy=_policy(),
        model_agent_max_concurrency=4,
        capacity=_capacity(cpu_percent=92, available_mib=1_024),
    )
    memory_limited = resolve_execution_profile(
        supreme_mode=True,
        policy=_policy(),
        model_agent_max_concurrency=4,
        capacity=_capacity(cpu_percent=18, available_mib=180),
    )

    assert cpu_limited.data_fetch_workers == 1
    assert cpu_limited.resource_level == "CRITICAL"
    assert "CPU_CRITICAL" in cpu_limited.reason_codes
    assert memory_limited.data_fetch_workers == 1
    assert "SUPREME_MODE_THROTTLED" in memory_limited.reason_codes


def test_standard_mode_remains_serial_even_when_capacity_is_available() -> None:
    profile = resolve_execution_profile(
        supreme_mode=False,
        policy=_policy(),
        model_agent_max_concurrency=4,
        capacity=_capacity(cpu_percent=18, available_mib=1_024),
    )

    assert profile.mode == "STANDARD"
    assert profile.data_fetch_workers == 1
    assert profile.model_agent_max_concurrency == 4
    assert "STANDARD_MODE" in profile.reason_codes
