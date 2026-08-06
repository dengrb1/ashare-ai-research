"""Energy-saving mode: a post-close, post-research quiescent state.

After the daily research run completes and the market is closed, the system may
enter a low-activity state: workers stop per-second queue polling, publish
``energy_saving`` heartbeats, and the host-side topology controller may stop the
optional services (searxng, idle workers) for the night.  The API, scheduler,
PostgreSQL and Redis stay up, so reads, auth and on-demand re-enable keep
working.

The mode is opt-in (``energy_saving_enabled`` system setting, default off) and
fully reversible:

* entering is automatic: enabled + after close + no in-flight jobs;
* exiting is automatic: any new research/backtest/trade-plan/exit-advice job,
  or the next scheduled dispatch, flips the state back to normal;
* an administrator can force-wake one cycle (``POST .../disable``) or re-arm
  auto-entry (``POST .../enable``).

The decision is deterministic and cheap: master switch + clock + four COUNT
queries.  Redis only records the live state (for observability) and the manual
wake marker.  Every Redis failure degrades to "stay awake", because an
energy-saving bug must never stop the workers from doing real work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ashare_ai.core.runtime_mode import is_after_close
from ashare_ai.storage.models import BacktestRun, ExitAdviceRow, JobRun, TradePlanRow

# Seconds a worker sleeps between wake-ups while energy saving is active.
DEEP_STANDBY_SECONDS = 60
# How often a worker re-evaluates the energy-saving decision while it is awake.
EVALUATION_INTERVAL_SECONDS = 30
# A force-wake marker expires after one research cycle so it cannot be forgotten.
MANUAL_WAKE_TTL_SECONDS = 12 * 60 * 60

_STATE_KEY = "ashare:energy-saving:state"
_MANUAL_KEY = "ashare:energy-saving:manual"
_MANUAL_WAKE = "wake"

# Mirror the orchestration queue constants: a DB row in one of these states is
# in-flight work that must keep the system awake.
_ACTIVE_RESEARCH_STATUSES = frozenset(
    {"PENDING", "QUEUED", "RUNNING", "PROCESSING", "DATA_READINESS_WAITING", "CANCEL_REQUESTED"}
)
_ACTIVE_WORK_STATUSES = frozenset({"PENDING", "QUEUED", "RUNNING", "PROCESSING"})


@dataclass(frozen=True)
class EnergySavingState:
    """Current energy-saving decision plus the observability record."""

    enabled: bool
    active: bool
    reason: str
    manual_wake: bool
    entered_at: datetime | None
    updated_at: datetime | None

    @property
    def deep_standby_seconds(self) -> int:
        return DEEP_STANDBY_SECONDS if self.active else 0


def active_work_reason(session: Session) -> tuple[int, str]:
    """Return (count, reason) of in-flight jobs that block energy saving.

    Research (``JobRun`` DAILY) uses its own readiness-aware status set; the
    other three async families share the same pending/running set.
    """
    research = int(
        session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.run_type == "DAILY",
                JobRun.status.in_(_ACTIVE_RESEARCH_STATUSES),
            )
        )
        or 0
    )
    if research:
        return research, f"{research} daily research run(s) still active"
    backtests = int(
        session.scalar(
            select(func.count())
            .select_from(BacktestRun)
            .where(BacktestRun.status.in_(_ACTIVE_WORK_STATUSES))
        )
        or 0
    )
    if backtests:
        return backtests, f"{backtests} backtest run(s) still active"
    trade_plans = int(
        session.scalar(
            select(func.count())
            .select_from(TradePlanRow)
            .where(TradePlanRow.status.in_(_ACTIVE_WORK_STATUSES))
        )
        or 0
    )
    if trade_plans:
        return trade_plans, f"{trade_plans} trade plan(s) still active"
    exit_advice = int(
        session.scalar(
            select(func.count())
            .select_from(ExitAdviceRow)
            .where(ExitAdviceRow.status.in_(_ACTIVE_WORK_STATUSES))
        )
        or 0
    )
    if exit_advice:
        return exit_advice, f"{exit_advice} exit advice task(s) still active"
    return 0, ""


def _decide(
    *,
    enabled: bool,
    manual_wake: bool,
    now: datetime,
    session: Session,
) -> tuple[bool, str]:
    if not enabled:
        return False, "energy saving is not enabled"
    if manual_wake:
        return False, "administrator forced a wake for this cycle"
    if not is_after_close(now):
        return False, "market session is still open"
    active, reason = active_work_reason(session)
    if active:
        return False, reason
    return True, "after close and all daily research complete"


def force_wake(redis_client: Any) -> None:
    """Wake the system for one cycle: workers resume immediately, controllers restart services."""
    try:
        redis_client.set(_MANUAL_KEY, _MANUAL_WAKE, ex=MANUAL_WAKE_TTL_SECONDS)
    except Exception:
        # A lost marker only means the system may re-enter energy saving on the
        # next evaluation; workers never treat Redis failure as a reason to stop.
        return


def rearm(redis_client: Any) -> None:
    """Re-arm auto-entry, clearing any force-wake marker."""
    try:
        redis_client.delete(_MANUAL_KEY)
    except Exception:
        return


def evaluate(
    *,
    redis_client: Any,
    session: Session,
    settings: Any,
    now: datetime | None = None,
) -> EnergySavingState:
    """Compute the current state and refresh the Redis observability record."""
    current = _aware(now or datetime.now(UTC))
    enabled = bool(getattr(settings, "energy_saving_enabled", False))
    manual_wake = _manual_wake_active(redis_client)
    active, reason = _decide(
        enabled=enabled, manual_wake=manual_wake, now=current, session=session
    )
    stored = _read_state(redis_client)
    stored_active = bool(stored.get("active")) if stored else False
    stored_entered_at = stored.get("entered_at")
    entered_at: str | None = None
    if active:
        if stored_active and isinstance(stored_entered_at, str):
            entered_at = stored_entered_at
        else:
            entered_at = current.isoformat()
    updated_at = current.isoformat()
    if not stored or stored_active != active or stored.get("reason") != reason:
        _write_state(
            redis_client,
            active=active,
            reason=reason,
            entered_at=entered_at,
            updated_at=updated_at,
        )
    return EnergySavingState(
        enabled=enabled,
        active=active,
        reason=reason,
        manual_wake=manual_wake,
        entered_at=_parse_iso(entered_at),
        updated_at=_parse_iso(updated_at),
    )


def _manual_wake_active(redis_client: Any) -> bool:
    try:
        marker = redis_client.get(_MANUAL_KEY)
    except Exception:
        return False
    if not isinstance(marker, (str, bytes)):
        return False
    value = marker.decode("utf-8") if isinstance(marker, bytes) else marker
    return value == _MANUAL_WAKE


def _read_state(redis_client: Any) -> dict[str, object]:
    try:
        raw = redis_client.get(_STATE_KEY)
    except Exception:
        return {}
    if not isinstance(raw, (str, bytes)):
        return {}
    try:
        parsed = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_state(
    redis_client: Any,
    *,
    active: bool,
    reason: str,
    entered_at: str | None,
    updated_at: str,
) -> None:
    payload = {
        "active": active,
        "reason": reason,
        "entered_at": entered_at,
        "updated_at": updated_at,
    }
    try:
        redis_client.set(_STATE_KEY, json.dumps(payload, separators=(",", ":")))
    except Exception:
        return


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_iso(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
