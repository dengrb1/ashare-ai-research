from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration.daily import auto_research_dispatch_flow

logger = logging.getLogger(__name__)


def seconds_until_next_tick(now: datetime, *, interval_minutes: int = 5) -> float:
    if now.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    if interval_minutes <= 0:
        raise ValueError("scheduler interval must be positive")
    localized = now.astimezone(SHANGHAI)
    current_minute = localized.replace(second=0, microsecond=0)
    minutes_to_add = interval_minutes - localized.minute % interval_minutes
    next_tick = current_minute + timedelta(minutes=minutes_to_add)
    return max(1.0, (next_tick - localized).total_seconds())


def run_scheduler_loop(
    *,
    dispatch: Callable[[], Any] = auto_research_dispatch_flow,
    now_factory: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
    sleep: Callable[[float], None] = time.sleep,
    max_iterations: int | None = None,
) -> None:
    completed = 0
    while max_iterations is None or completed < max_iterations:
        try:
            dispatch()
        except Exception:
            logger.exception("automatic research dispatch failed; retrying at next tick")
        completed += 1
        if max_iterations is not None and completed >= max_iterations:
            return
        sleep(seconds_until_next_tick(now_factory()))


def main() -> None:
    run_scheduler_loop()


if __name__ == "__main__":
    main()
