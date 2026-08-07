"""Bounded, best-effort memory reclamation for long-lived Python services."""

from __future__ import annotations

import ctypes
import gc
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psutil

MIB = 1024**2
DEFAULT_MINIMUM_RSS_BYTES = 160 * MIB
DEFAULT_COOLDOWN_SECONDS = 300


@dataclass(frozen=True)
class MemoryReclaimReport:
    reason: str
    attempted: bool
    skipped_reason: str | None
    rss_before_bytes: int | None
    rss_after_bytes: int | None
    collected_objects: int
    allocator_trimmed: bool

    @property
    def reclaimed_bytes(self) -> int:
        if self.rss_before_bytes is None or self.rss_after_bytes is None:
            return 0
        return max(0, self.rss_before_bytes - self.rss_after_bytes)


class ProcessMemoryReclaimer:
    """Run full GC and allocator trimming only at explicit lifecycle boundaries."""

    def __init__(
        self,
        *,
        process: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        collector: Callable[[], int] = gc.collect,
        allocator_trim: Callable[[], bool] | None = None,
    ) -> None:
        self._process = process or psutil.Process()
        self._clock = clock
        self._collector = collector
        self._allocator_trim = allocator_trim or _trim_allocator
        self._last_reclaim_at: float | None = None
        self._lock = threading.Lock()

    def reclaim(
        self,
        *,
        reason: str,
        enabled: bool = True,
        minimum_rss_bytes: int = DEFAULT_MINIMUM_RSS_BYTES,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        force: bool = False,
    ) -> MemoryReclaimReport:
        if minimum_rss_bytes < 0:
            raise ValueError("minimum_rss_bytes cannot be negative")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        with self._lock:
            before = self._rss()
            if not enabled:
                return self._skipped(reason, "disabled", before)
            now = self._clock()
            if (
                self._last_reclaim_at is not None
                and now - self._last_reclaim_at < cooldown_seconds
            ):
                return self._skipped(reason, "cooldown", before)
            if not force and before is not None and before < minimum_rss_bytes:
                return self._skipped(reason, "below-threshold", before)

            collected = self._collector()
            trimmed = self._allocator_trim()
            self._last_reclaim_at = now
            return MemoryReclaimReport(
                reason=reason,
                attempted=True,
                skipped_reason=None,
                rss_before_bytes=before,
                rss_after_bytes=self._rss(),
                collected_objects=max(0, int(collected)),
                allocator_trimmed=trimmed,
            )

    def _rss(self) -> int | None:
        try:
            return int(self._process.memory_info().rss)
        except (OSError, psutil.Error):
            return None

    @staticmethod
    def _skipped(
        reason: str, skipped_reason: str, rss: int | None
    ) -> MemoryReclaimReport:
        return MemoryReclaimReport(
            reason=reason,
            attempted=False,
            skipped_reason=skipped_reason,
            rss_before_bytes=rss,
            rss_after_bytes=rss,
            collected_objects=0,
            allocator_trimmed=False,
        )


def _trim_allocator() -> bool:
    """Return free glibc arenas to the OS; safely no-op elsewhere."""
    if os.name != "posix":
        return False
    try:
        malloc_trim = ctypes.CDLL(None).malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        return bool(malloc_trim(0))
    except (AttributeError, OSError):
        return False


_PROCESS_RECLAIMER = ProcessMemoryReclaimer()


def reclaim_runtime_memory(
    settings: Any,
    *,
    reason: str,
    force: bool = False,
) -> MemoryReclaimReport:
    """Apply environment-backed policy without coupling workers to Settings."""
    return _PROCESS_RECLAIMER.reclaim(
        reason=reason,
        enabled=bool(getattr(settings, "memory_reclaim_enabled", True)),
        minimum_rss_bytes=(
            int(getattr(settings, "memory_reclaim_min_rss_mib", 160)) * MIB
        ),
        cooldown_seconds=int(
            getattr(settings, "memory_reclaim_cooldown_seconds", 300)
        ),
        force=force,
    )
