"""Runtime resource profiles for the API process.

The research workers keep their own execution policy.  This module only
controls live-data resources owned by the API process, so switching profiles
does not change point-in-time research or model-gateway semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from ashare_ai.core.config import Settings

ApiRuntimeMode = Literal["LIGHTWEIGHT", "SUPREME"]
RuntimeMemoryStrategy = Literal["LOW_RESIDENT", "MAX_THROUGHPUT"]

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class RuntimeModePolicy:
    mode: ApiRuntimeMode
    memory_strategy: RuntimeMemoryStrategy
    use_isolated_akshare: bool
    market_cache_max_entries: int
    market_prefetch_max_workers: int
    market_provider_max_workers: int
    market_provider_max_queue: int
    auto_close_after_close: bool

    @property
    def primary_provider(self) -> str:
        return "akshare" if self.use_isolated_akshare else "sina"

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "memory_strategy": self.memory_strategy,
            "use_isolated_akshare": self.use_isolated_akshare,
            "primary_provider": self.primary_provider,
            "market_cache_max_entries": self.market_cache_max_entries,
            "market_prefetch_max_workers": self.market_prefetch_max_workers,
            "market_provider_max_workers": self.market_provider_max_workers,
            "market_provider_max_queue": self.market_provider_max_queue,
            "auto_close_after_close": self.auto_close_after_close,
        }


def normalize_runtime_mode(value: object) -> ApiRuntimeMode:
    normalized = str(value or "").strip().upper()
    if normalized not in {"LIGHTWEIGHT", "SUPREME"}:
        raise ValueError("api runtime mode must be LIGHTWEIGHT or SUPREME")
    return normalized  # type: ignore[return-value]


def configured_runtime_mode(settings: Settings) -> ApiRuntimeMode:
    return normalize_runtime_mode(getattr(settings, "api_runtime_mode", "LIGHTWEIGHT"))


def runtime_mode_policy(settings: Settings) -> RuntimeModePolicy:
    mode = configured_runtime_mode(settings)
    if mode == "LIGHTWEIGHT":
        return RuntimeModePolicy(
            mode=mode,
            memory_strategy="LOW_RESIDENT",
            use_isolated_akshare=False,
            market_cache_max_entries=min(settings.market_cache_max_entries, 128),
            market_prefetch_max_workers=1,
            market_provider_max_workers=1,
            market_provider_max_queue=min(settings.market_provider_max_queue, 2),
            auto_close_after_close=bool(settings.api_runtime_auto_close),
        )
    return RuntimeModePolicy(
        mode=mode,
        memory_strategy="MAX_THROUGHPUT",
        use_isolated_akshare=True,
        market_cache_max_entries=settings.market_cache_max_entries,
        market_prefetch_max_workers=settings.market_prefetch_max_workers,
        market_provider_max_workers=settings.market_provider_max_workers,
        market_provider_max_queue=settings.market_provider_max_queue,
        auto_close_after_close=bool(settings.api_runtime_auto_close),
    )


def is_after_close(now: datetime | None = None) -> bool:
    """Return a calendar-free close hint used only for resource reclamation."""

    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    return current.weekday() >= 5 or current.time() >= time(15, 0)
