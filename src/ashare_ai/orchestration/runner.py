from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ashare_ai.core.time import SHANGHAI

logger = logging.getLogger(__name__)


def dispatch_auto_research_once() -> dict[str, Any]:
    """Run the lightweight scheduler check without starting a Prefect API server."""
    from ashare_ai.orchestration.research_schedule import dispatch_auto_research

    return dispatch_auto_research()


def dispatch_scheduled_tasks() -> dict[str, Any]:
    from ashare_ai.notifications.service import NotificationService
    from ashare_ai.orchestration.exit_advice_jobs import dispatch_exit_advice
    from ashare_ai.portfolio.monitoring import BuyEntryMonitorService, StopLossMonitorService
    from ashare_ai.storage.database import SessionLocal

    now = datetime.now(SHANGHAI)
    buy_monitor = BuyEntryMonitorService()
    refresh_created = 0
    # The row uniqueness constraint makes this safe during retries; limiting the
    # refresh to after close avoids treating intraday plans as formal inputs.
    if now.time().replace(tzinfo=None) >= datetime.strptime("15:05", "%H:%M").time():
        refresh_created = buy_monitor.refresh_from_trade_plans(now=now)
    with SessionLocal() as session:
        expired_notifications = NotificationService(session).cleanup_expired(now=now)
        if expired_notifications:
            session.commit()

    return {
        "exit_advice": dispatch_exit_advice(now=now),
        "stop_loss": StopLossMonitorService().dispatch(now=now),
        "buy_entry_monitor": buy_monitor.dispatch(now=now),
        "buy_entry_refresh_created": refresh_created,
        "expired_notifications": expired_notifications,
        "daily_research": dispatch_auto_research_once(),
    }


def seconds_until_next_tick(now: datetime, *, interval_minutes: int = 1) -> float:
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
    dispatch: Callable[[], Any] = dispatch_scheduled_tasks,
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
