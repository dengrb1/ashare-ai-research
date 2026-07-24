from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Sellability:
    held_quantity: int
    acquired_on: date | None
    sellable_quantity: int
    blockers: tuple[str, ...]

    @property
    def t1_restricted(self) -> bool:
        return "T1_NOT_SELLABLE" in self.blockers


def position_sellability(position: dict[str, Any], *, trading_date: date) -> Sellability:
    """Return fail-closed T+1 eligibility for one persisted paper position."""

    quantity = max(0, int(position.get("quantity", 0)))
    raw = position.get("acquired_on")
    if not raw:
        return Sellability(quantity, None, 0, ("MISSING_ACQUIRED_ON",))
    try:
        acquired_on = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return Sellability(quantity, None, 0, ("MISSING_ACQUIRED_ON",))
    if acquired_on >= trading_date:
        return Sellability(quantity, acquired_on, 0, ("T1_NOT_SELLABLE",))
    return Sellability(quantity, acquired_on, quantity, ())
