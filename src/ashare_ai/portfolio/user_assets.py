from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
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
    },
    {
        "symbol": "300750.SZ",
        "name": "宁德时代",
        "quantity": 500,
        "cost": 241.2,
    },
    {
        "symbol": "601318.SH",
        "name": "中国平安",
        "quantity": 1200,
        "cost": 52.16,
    },
]

class _UnsetTotalAssets:
    pass


UNSET_TOTAL_ASSETS = _UnsetTotalAssets()


class UserAssetService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: str) -> dict[str, Any]:
        row = self.session.get(UserAssetState, user_id)
        if row is None:
            return {
                "watchlist": list(DEFAULT_WATCHLIST),
                "positions": [dict(position) for position in DEFAULT_POSITIONS],
                "total_assets": None,
                "exit_monitor_enabled": False,
                "default_profit_trigger": None,
                "stop_loss_monitor_enabled": True,
                "buy_monitor_enabled": True,
                "market_refresh_interval_seconds": 5,
                "updated_at": None,
            }
        return {
            "watchlist": list(row.watchlist),
            "positions": [dict(position) for position in row.positions],
            "total_assets": float(row.total_assets) if row.total_assets is not None else None,
            "exit_monitor_enabled": row.exit_monitor_enabled,
            "default_profit_trigger": (
                float(row.default_profit_trigger)
                if row.default_profit_trigger is not None
                else None
            ),
            "stop_loss_monitor_enabled": row.stop_loss_monitor_enabled,
            "buy_monitor_enabled": row.buy_monitor_enabled,
            "market_refresh_interval_seconds": row.market_refresh_interval_seconds,
            "updated_at": row.updated_at,
        }

    def save(
        self,
        user_id: str,
        watchlist: list[str],
        positions: list[dict[str, Any]],
        total_assets: float | None | _UnsetTotalAssets = UNSET_TOTAL_ASSETS,
        exit_monitor_enabled: bool = False,
        default_profit_trigger: float | None = None,
        stop_loss_monitor_enabled: bool | None = None,
        buy_monitor_enabled: bool | None = None,
        market_refresh_interval_seconds: int | None = None,
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        row = self.session.get(UserAssetState, user_id)
        if row is None:
            row = UserAssetState(user_id=user_id, updated_at=now)
            self.session.add(row)
        row.watchlist = list(watchlist)
        row.positions = [dict(position) for position in positions]
        if not isinstance(total_assets, _UnsetTotalAssets):
            row.total_assets = (
                Decimal(str(total_assets)) if total_assets is not None else None
            )
        row.exit_monitor_enabled = exit_monitor_enabled
        row.default_profit_trigger = (
            Decimal(str(default_profit_trigger))
            if default_profit_trigger is not None
            else None
        )
        if stop_loss_monitor_enabled is not None:
            row.stop_loss_monitor_enabled = stop_loss_monitor_enabled
        if buy_monitor_enabled is not None:
            row.buy_monitor_enabled = buy_monitor_enabled
        if market_refresh_interval_seconds is not None:
            row.market_refresh_interval_seconds = market_refresh_interval_seconds
        row.updated_at = now
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return self.get(user_id)
