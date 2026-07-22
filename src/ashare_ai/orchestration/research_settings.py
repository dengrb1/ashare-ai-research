from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.storage.models import AutomaticResearchReportConfig, UserResearchPreference

DEFAULT_TOTAL_BUDGET = Decimal("1000000")
DEFAULT_PER_SYMBOL_BUDGET = Decimal("80000")
SLOTS = ("A", "B")


class ResearchSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: str) -> dict[str, Any]:
        row = self.session.get(UserResearchPreference, user_id)
        stored = {
            item.slot: item
            for item in self.session.scalars(
                select(AutomaticResearchReportConfig).where(
                    AutomaticResearchReportConfig.user_id == user_id
                )
            ).all()
        }
        reports = [self._report(slot, stored.get(slot), row) for slot in SLOTS]
        first = reports[0]
        return {
            "auto_enabled": any(item["enabled"] for item in reports),
            "updated_at": row.updated_at if row is not None else None,
            "automatic_scope": first["scope"],
            "automatic_total_budget": self._compat_number(first["total_budget"]),
            "automatic_per_symbol_budget": self._compat_number(first["per_symbol_budget"]),
            "automatic_max_stock_price": self._compat_number(first["max_stock_price"]),
            "automatic_reports": reports,
            "schedule_timezone": "Asia/Shanghai",
            "schedule_time": "15:05",
            "snapshot_mode": "SYSTEM_ENFORCED",
        }

    def update(
        self,
        user_id: str,
        *,
        auto_enabled: bool | None = None,
        automatic_reports: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        row = self.session.get(UserResearchPreference, user_id)
        now = datetime.now(UTC)
        enabled = bool(auto_enabled)
        if automatic_reports is not None:
            enabled = any(bool(item["enabled"]) for item in automatic_reports)
        if row is None:
            row = UserResearchPreference(
                user_id=user_id,
                auto_enabled=enabled,
                updated_at=now,
            )
            self.session.add(row)
        else:
            row.auto_enabled = enabled
            row.updated_at = now
        stored = {
            item.slot: item
            for item in self.session.scalars(
                select(AutomaticResearchReportConfig).where(
                    AutomaticResearchReportConfig.user_id == user_id
                )
            ).all()
        }
        if automatic_reports is not None:
            incoming = {str(item["slot"]): item for item in automatic_reports}
            for slot in SLOTS:
                self._write_config(user_id, slot, incoming[slot], stored.get(slot), now)
        elif auto_enabled is not None:
            # Legacy writes preserve saved values. Enabling selects report A;
            # disabling turns both slots off.
            for slot in SLOTS:
                existing = stored.get(slot)
                values = self._report(slot, existing, row)
                values["enabled"] = bool(auto_enabled) if slot == "A" else False
                self._write_config(user_id, slot, values, existing, now)
        self.session.commit()
        return self.get(user_id)

    @staticmethod
    def _report(
        slot: str,
        row: AutomaticResearchReportConfig | None,
        preference: UserResearchPreference | None,
    ) -> dict[str, Any]:
        return {
            "slot": slot,
            "enabled": (
                bool(row.enabled)
                if row is not None
                else bool(preference and preference.auto_enabled and slot == "A")
            ),
            "scope": row.scope if row is not None else "MARKET",
            "symbols": list(row.symbols) if row is not None else [],
            "total_budget": row.total_budget if row is not None else DEFAULT_TOTAL_BUDGET,
            "per_symbol_budget": (
                row.per_symbol_budget if row is not None else DEFAULT_PER_SYMBOL_BUDGET
            ),
            "max_stock_price": row.max_stock_price if row is not None else None,
            "config_version": row.config_version if row is not None else 1,
        }

    def _write_config(
        self,
        user_id: str,
        slot: str,
        values: dict[str, Any],
        row: AutomaticResearchReportConfig | None,
        now: datetime,
    ) -> None:
        if row is None:
            row = AutomaticResearchReportConfig(
                user_id=user_id,
                slot=slot,
                total_budget=Decimal(str(values["total_budget"])),
                per_symbol_budget=Decimal(str(values["per_symbol_budget"])),
                updated_at=now,
            )
            self.session.add(row)
        else:
            row.config_version += 1
        row.enabled = bool(values["enabled"])
        row.scope = str(values["scope"])
        row.symbols = list(values.get("symbols") or [])
        row.total_budget = Decimal(str(values["total_budget"]))
        row.per_symbol_budget = Decimal(str(values["per_symbol_budget"]))
        maximum = values.get("max_stock_price")
        row.max_stock_price = Decimal(str(maximum)) if maximum is not None else None
        row.updated_at = now

    @staticmethod
    def _compat_number(value: Any) -> Any:
        if isinstance(value, Decimal) and value == value.to_integral_value():
            return int(value)
        return value
