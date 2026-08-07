from __future__ import annotations

from types import SimpleNamespace

from ashare_ai.observability.memory_reclaimer import ProcessMemoryReclaimer


class _Process:
    def __init__(self, rss: int) -> None:
        self.rss = rss

    def memory_info(self) -> SimpleNamespace:
        return SimpleNamespace(rss=self.rss)


def test_reclaimer_collects_and_reports_released_rss() -> None:
    process = _Process(240)
    events: list[str] = []

    def collect() -> int:
        events.append("gc")
        process.rss = 120
        return 7

    def trim_allocator() -> bool:
        events.append("trim")
        return True

    reclaimer = ProcessMemoryReclaimer(
        process=process,
        clock=lambda: 100.0,
        collector=collect,
        allocator_trim=trim_allocator,
    )
    report = reclaimer.reclaim(
        reason="test", minimum_rss_bytes=160, cooldown_seconds=30
    )

    assert events == ["gc", "trim"]
    assert report.attempted is True
    assert report.collected_objects == 7
    assert report.allocator_trimmed is True
    assert report.reclaimed_bytes == 120


def test_reclaimer_honors_threshold_cooldown_and_force() -> None:
    process = _Process(100)
    clock = [100.0]
    calls: list[str] = []
    def collect() -> int:
        calls.append("gc")
        return 0

    reclaimer = ProcessMemoryReclaimer(
        process=process,
        clock=lambda: clock[0],
        collector=collect,
        allocator_trim=lambda: False,
    )

    below = reclaimer.reclaim(
        reason="below", minimum_rss_bytes=160, cooldown_seconds=30
    )
    forced = reclaimer.reclaim(
        reason="forced", minimum_rss_bytes=160, cooldown_seconds=30, force=True
    )
    clock[0] = 110.0
    cooling_down = reclaimer.reclaim(
        reason="cooldown", minimum_rss_bytes=0, cooldown_seconds=30
    )

    assert below.skipped_reason == "below-threshold"
    assert forced.attempted is True
    assert cooling_down.skipped_reason == "cooldown"
    assert calls == ["gc"]


def test_reclaimer_can_be_disabled() -> None:
    reclaimer = ProcessMemoryReclaimer(
        process=_Process(500),
        collector=lambda: (_ for _ in ()).throw(AssertionError("must not collect")),
    )
    report = reclaimer.reclaim(reason="disabled", enabled=False)
    assert report.attempted is False
    assert report.skipped_reason == "disabled"
