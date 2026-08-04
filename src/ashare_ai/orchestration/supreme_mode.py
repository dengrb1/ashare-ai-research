"""Adaptive, auditable data-acquisition profile for a single research run."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_ai.core.hashing import sha256_bytes
from ashare_ai.observability.runtime_resources import MIB, cgroup_memory_details


class SupremeModePolicy(BaseModel):
    """Versioned guardrails for the per-run adaptive fetch pool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=64)
    max_data_fetch_workers: int = Field(ge=1, le=16)
    estimated_memory_per_fetch_worker_mib: int = Field(ge=16, le=256)
    reserve_memory_mib: int = Field(ge=64, le=1024)
    cpu_warning_percent: int = Field(ge=50, le=95)
    cpu_critical_percent: int = Field(ge=75, le=100)

    @model_validator(mode="after")
    def validate_thresholds(self) -> SupremeModePolicy:
        if self.cpu_critical_percent <= self.cpu_warning_percent:
            raise ValueError("cpu_critical_percent must exceed cpu_warning_percent")
        return self


@dataclass(frozen=True)
class RuntimeCapacity:
    scope: Literal["HOST", "CONTAINER"]
    logical_cores: int
    cpu_percent: float
    available_memory_bytes: int
    memory_limit_bytes: int | None
    active_memory_bytes: int | None


@dataclass(frozen=True)
class ResearchExecutionProfile:
    policy_version: str
    mode: Literal["STANDARD", "SUPREME"]
    data_fetch_workers: int
    model_agent_max_concurrency: int
    resource_scope: Literal["HOST", "CONTAINER"]
    logical_cores: int
    cpu_percent: float
    available_memory_bytes: int
    memory_limit_bytes: int | None
    active_memory_bytes: int | None
    memory_budget_bytes: int
    resource_level: Literal["NORMAL", "WARNING", "CRITICAL"]
    reason_codes: tuple[str, ...]

    def manifest_value(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "mode": self.mode,
            "data_fetch_workers": self.data_fetch_workers,
            "model_agent_max_concurrency": self.model_agent_max_concurrency,
            "model_concurrency_changed": False,
            "resource_scope": self.resource_scope,
            "logical_cores": self.logical_cores,
            "cpu_percent": self.cpu_percent,
            "available_memory_bytes": self.available_memory_bytes,
            "memory_limit_bytes": self.memory_limit_bytes,
            "active_memory_bytes": self.active_memory_bytes,
            "memory_budget_bytes": self.memory_budget_bytes,
            "resource_level": self.resource_level,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_manifest(cls, value: object) -> ResearchExecutionProfile | None:
        if not isinstance(value, dict):
            return None
        try:
            mode = str(value["mode"]).upper()
            level = str(value["resource_level"]).upper()
            if mode not in {"STANDARD", "SUPREME"} or level not in {
                "NORMAL",
                "WARNING",
                "CRITICAL",
            }:
                return None
            return cls(
                policy_version=str(value["policy_version"]),
                mode=mode,  # type: ignore[arg-type]
                data_fetch_workers=max(1, int(value["data_fetch_workers"])),
                model_agent_max_concurrency=max(
                    1, int(value["model_agent_max_concurrency"])
                ),
                resource_scope=(
                    "CONTAINER" if value.get("resource_scope") == "CONTAINER" else "HOST"
                ),
                logical_cores=max(1, int(value["logical_cores"])),
                cpu_percent=max(0.0, float(value["cpu_percent"])),
                available_memory_bytes=max(0, int(value["available_memory_bytes"])),
                memory_limit_bytes=(
                    int(value["memory_limit_bytes"])
                    if value.get("memory_limit_bytes") is not None
                    else None
                ),
                active_memory_bytes=(
                    int(value["active_memory_bytes"])
                    if value.get("active_memory_bytes") is not None
                    else None
                ),
                memory_budget_bytes=max(0, int(value["memory_budget_bytes"])),
                resource_level=level,  # type: ignore[arg-type]
                reason_codes=tuple(str(item) for item in value.get("reason_codes", [])),
            )
        except (KeyError, TypeError, ValueError):
            return None


def load_supreme_mode_policy(path: Path) -> tuple[SupremeModePolicy, str]:
    payload = path.read_bytes()
    return SupremeModePolicy.model_validate_json(payload), sha256_bytes(payload)


def _cgroup_cpu_count(roots: tuple[Path, ...] = (Path("/sys/fs/cgroup"),)) -> int | None:
    for root in roots:
        try:
            quota, period = (root / "cpu.max").read_text(encoding="ascii").split()[:2]
            if quota != "max":
                return max(1, math.ceil(int(quota) / int(period)))
        except (OSError, ValueError, ZeroDivisionError):
            pass
        try:
            quota_value = int(
                (root / "cpu/cpu.cfs_quota_us").read_text(encoding="ascii").strip()
            )
            period_value = int(
                (root / "cpu/cpu.cfs_period_us").read_text(encoding="ascii").strip()
            )
            if quota_value > 0:
                return max(1, math.ceil(quota_value / period_value))
        except (OSError, ValueError, ZeroDivisionError):
            pass
    return None


def sample_runtime_capacity() -> RuntimeCapacity:
    """Read only bounded process/container metrics; no Docker socket is used."""

    memory = psutil.virtual_memory()
    # A short interval avoids treating psutil's first non-blocking sample as real idle time.
    cpu_percent = max(0.0, round(float(psutil.cpu_percent(interval=0.1)), 1))
    usage, limit, inactive_file = cgroup_memory_details()
    active_memory = max(0, usage - inactive_file) if usage is not None else None
    cgroup_available = (
        max(0, limit - active_memory)
        if limit is not None and active_memory is not None
        else None
    )
    available_memory = min(
        int(memory.available), cgroup_available
    ) if cgroup_available is not None else int(memory.available)
    cgroup_cores = _cgroup_cpu_count()
    logical_cores = max(1, min(int(psutil.cpu_count(logical=True) or 1), cgroup_cores or 2**31))
    return RuntimeCapacity(
        scope="CONTAINER" if limit is not None or Path("/.dockerenv").exists() else "HOST",
        logical_cores=logical_cores,
        cpu_percent=cpu_percent,
        available_memory_bytes=max(0, available_memory),
        memory_limit_bytes=limit,
        active_memory_bytes=active_memory,
    )


def resolve_execution_profile(
    *,
    supreme_mode: bool,
    policy: SupremeModePolicy,
    model_agent_max_concurrency: int,
    capacity: RuntimeCapacity | None = None,
) -> ResearchExecutionProfile:
    """Select data I/O parallelism without changing model-gateway pressure."""

    runtime = capacity or sample_runtime_capacity()
    reserve = policy.reserve_memory_mib * MIB
    estimated_per_worker = policy.estimated_memory_per_fetch_worker_mib * MIB
    budget = max(0, runtime.available_memory_bytes - reserve)
    reasons: list[str] = []
    level: Literal["NORMAL", "WARNING", "CRITICAL"] = "NORMAL"

    if runtime.cpu_percent >= policy.cpu_critical_percent:
        level = "CRITICAL"
        reasons.append("CPU_CRITICAL")
    elif runtime.cpu_percent >= policy.cpu_warning_percent:
        level = "WARNING"
        reasons.append("CPU_HIGH")
    if runtime.available_memory_bytes <= reserve:
        level = "CRITICAL"
        reasons.append("MEMORY_HEADROOM_CRITICAL")
    elif budget < estimated_per_worker * 2 and level == "NORMAL":
        level = "WARNING"
        reasons.append("MEMORY_HEADROOM_LOW")

    if not supreme_mode:
        reasons.append("STANDARD_MODE")
        workers = 1
    else:
        by_memory = max(1, budget // estimated_per_worker)
        by_cpu = max(1, runtime.logical_cores * 2)
        if runtime.cpu_percent >= policy.cpu_critical_percent:
            by_pressure = 1
        elif runtime.cpu_percent >= policy.cpu_warning_percent:
            by_pressure = max(2, runtime.logical_cores // 2)
        else:
            by_pressure = policy.max_data_fetch_workers
        workers = max(
            1,
            min(policy.max_data_fetch_workers, by_memory, by_cpu, by_pressure),
        )
        if workers == 1:
            reasons.append("SUPREME_MODE_THROTTLED")
        else:
            reasons.append("SUPREME_MODE_ACTIVE")

    return ResearchExecutionProfile(
        policy_version=policy.version,
        mode="SUPREME" if supreme_mode else "STANDARD",
        data_fetch_workers=int(workers),
        model_agent_max_concurrency=max(1, int(model_agent_max_concurrency)),
        resource_scope=runtime.scope,
        logical_cores=runtime.logical_cores,
        cpu_percent=runtime.cpu_percent,
        available_memory_bytes=runtime.available_memory_bytes,
        memory_limit_bytes=runtime.memory_limit_bytes,
        active_memory_bytes=runtime.active_memory_bytes,
        memory_budget_bytes=budget,
        resource_level=level,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
