from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ashare_ai.storage.models import UserResearchPreference


class ResearchSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: str) -> dict[str, Any]:
        row = self.session.get(UserResearchPreference, user_id)
        return {
            "auto_enabled": bool(row.auto_enabled) if row is not None else False,
            "updated_at": row.updated_at if row is not None else None,
            "automatic_scope": "MARKET",
            "automatic_total_budget": 1_000_000,
            "automatic_per_symbol_budget": 80_000,
            "automatic_max_stock_price": None,
            "schedule_timezone": "Asia/Shanghai",
            "schedule_time": "15:05",
            "snapshot_mode": "SYSTEM_ENFORCED",
        }

    def update(self, user_id: str, *, auto_enabled: bool) -> dict[str, Any]:
        row = self.session.get(UserResearchPreference, user_id)
        now = datetime.now(UTC)
        if row is None:
            row = UserResearchPreference(
                user_id=user_id,
                auto_enabled=auto_enabled,
                updated_at=now,
            )
            self.session.add(row)
        else:
            row.auto_enabled = auto_enabled
            row.updated_at = now
        self.session.commit()
        return self.get(user_id)
