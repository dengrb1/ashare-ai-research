from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ashare_ai.storage.models import UserAssetState

DEFAULT_WATCHLIST = ["600519.SH", "000858.SZ", "300750.SZ", "601318.SH"]
DEFAULT_POSITIONS: list[dict[str, Any]] = [
    {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "quantity": 100,
        "cost": 1428.5,
        "target_weight": 0.32,
    },
    {
        "symbol": "300750.SZ",
        "name": "宁德时代",
        "quantity": 500,
        "cost": 241.2,
        "target_weight": 0.28,
    },
    {
        "symbol": "601318.SH",
        "name": "中国平安",
        "quantity": 1200,
        "cost": 52.16,
        "target_weight": 0.22,
    },
]


class UserAssetService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: str) -> dict[str, Any]:
        row = self.session.get(UserAssetState, user_id)
        if row is None:
            return {
                "watchlist": list(DEFAULT_WATCHLIST),
                "positions": [dict(position) for position in DEFAULT_POSITIONS],
                "updated_at": None,
            }
        return {
            "watchlist": list(row.watchlist),
            "positions": [dict(position) for position in row.positions],
            "updated_at": row.updated_at,
        }

    def save(
        self,
        user_id: str,
        watchlist: list[str],
        positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        row = self.session.get(UserAssetState, user_id)
        if row is None:
            row = UserAssetState(user_id=user_id, updated_at=now)
            self.session.add(row)
        row.watchlist = list(watchlist)
        row.positions = [dict(position) for position in positions]
        row.updated_at = now
        self.session.commit()
        return self.get(user_id)
