from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.agents.model_settings import ModelConfigurationService
from ashare_ai.core.time import SHANGHAI
from ashare_ai.market.service import get_market_data_service
from ashare_ai.notifications.service import NotificationService
from ashare_ai.portfolio.monitoring import derive_stop_loss_price
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import TradeAdviceMonitorRow, UserAssetState


def _price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class TradeAdviceService:
    """Daily target materialisation and intraday notification dispatch.

    The persisted result has a stable deterministic fallback, while model identity
    is captured from the active administrator configuration. This keeps alerts
    fail-closed when a configured model is temporarily unavailable.
    """

    def __init__(
        self, *, market: Any | None = None, session_factory: Callable[[], Session] = SessionLocal
    ) -> None:
        self.market = market or get_market_data_service()
        self.session_factory = session_factory

    def generate_daily(self, *, now: datetime) -> int:
        current = now.astimezone(SHANGHAI)
        if current.time() < time(9, 30) or current.time() > time(15, 0):
            return 0
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(TradeAdviceMonitorRow).where(TradeAdviceMonitorRow.enabled.is_(True))
                ).all()
            )
            symbols = sorted({row.symbol for row in rows if row.generated_for != current.date()})
            quotes = (
                {
                    str(item.get("symbol")): item
                    for item in self.market.quotes(symbols)
                    if isinstance(item, dict)
                }
                if symbols
                else {}
            )
            runtime = ModelConfigurationService().resolve(session, require_enabled=False)
            source = "RESEARCH"
            model = None
            reasoning = None
            config_hash = None
            if runtime is not None:
                model, reasoning = runtime.model_for("research")
                config_hash = runtime.config_sha256
            changed = 0
            for row in rows:
                if row.generated_for == current.date():
                    continue
                quote = quotes.get(row.symbol)
                if quote is None or quote.get("price") is None:
                    row.error_code = "QUOTE_UNAVAILABLE"
                    row.updated_at = current.astimezone(UTC)
                    continue
                price = Decimal(str(quote["price"]))
                change_pct = abs(Decimal(str(quote.get("change_pct") or 0)))
                # Higher intraday momentum receives a wider upside target; quiet
                # stocks receive closer targets. The model configuration is saved
                # so a later AI enrichment can be audited without changing alerts.
                upside = min(
                    Decimal("0.12"),
                    Decimal("0.035") + change_pct / Decimal("100") * Decimal("0.75"),
                )
                downside = min(
                    Decimal("0.10"),
                    Decimal("0.025") + change_pct / Decimal("100") * Decimal("0.45"),
                )
                row.ai_buy_price = _price(price * (Decimal("1") - downside))
                row.ai_sell_price = _price(price * (Decimal("1") + upside))
                state = session.get(UserAssetState, row.user_id)
                position = next(
                    (
                        item
                        for item in (state.positions if state else [])
                        if item.get("symbol") == row.symbol
                    ),
                    None,
                )
                row.stop_loss_price = derive_stop_loss_price(position).price if position else None
                row.rationale = {
                    "summary": "依据开盘后实时价格、日内波动与持仓风险生成；仅供模拟参考。",
                    "momentum_pct": str(change_pct),
                    "reasoning_effort": reasoning,
                }
                row.generated_for, row.generated_at = current.date(), current.astimezone(UTC)
                row.model_name, row.model_source, row.model_config_sha256 = (
                    model,
                    source,
                    config_hash,
                )
                row.error_code, row.updated_at = None, current.astimezone(UTC)
                changed += 1
            if changed:
                session.commit()
            return changed

    def dispatch(self, *, now: datetime) -> list[str]:
        current = now.astimezone(SHANGHAI)
        if not (time(9, 30) <= current.time() <= time(15, 0)):
            return []
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(TradeAdviceMonitorRow).where(TradeAdviceMonitorRow.enabled.is_(True))
                ).all()
            )
            quotes = (
                {
                    str(item.get("symbol")): item
                    for item in self.market.quotes(sorted({r.symbol for r in rows}))
                    if isinstance(item, dict)
                }
                if rows
                else {}
            )
            notifications = NotificationService(session)
            sent: list[str] = []
            for row in rows:
                quote = quotes.get(row.symbol)
                if quote is None or quote.get("price") is None or row.error_code:
                    continue
                price = Decimal(str(quote["price"]))
                targets = [
                    (
                        "BUY_TARGET_HIT",
                        row.manual_buy_price or row.ai_buy_price,
                        price <= (row.manual_buy_price or row.ai_buy_price)
                        if (row.manual_buy_price or row.ai_buy_price)
                        else False,
                    ),
                    (
                        "SELL_TARGET_HIT",
                        row.manual_sell_price or row.ai_sell_price,
                        price >= (row.manual_sell_price or row.ai_sell_price)
                        if (row.manual_sell_price or row.ai_sell_price)
                        else False,
                    ),
                    (
                        "STOP_LOSS_TRIGGERED",
                        row.stop_loss_price,
                        price <= row.stop_loss_price if row.stop_loss_price else False,
                    ),
                ]
                hits = [
                    (kind, target) for kind, target, hit in targets if hit and target is not None
                ]
                if not hits or (
                    row.last_alert_at
                    and current.astimezone(UTC) - row.last_alert_at < timedelta(minutes=5)
                ):
                    continue
                kinds: list[str] = []
                for kind, target in hits:
                    label = {
                        "BUY_TARGET_HIT": "买入目标",
                        "SELL_TARGET_HIT": "卖出目标",
                        "STOP_LOSS_TRIGGERED": "止损",
                    }[kind]
                    notification = notifications.create(
                        user_id=row.user_id,
                        notification_type=kind,
                        severity="CRITICAL" if kind == "STOP_LOSS_TRIGGERED" else "HIGH",
                        title=f"{row.symbol} {label}已触发",
                        body=f"最新价 {price}，目标价 {target}。仅提醒，不自动交易。",
                        resource_type="TRADE_ADVICE",
                        resource_id=row.monitor_id,
                        payload={"symbol": row.symbol, "target": str(target)},
                        dedupe_key=f"trade-advice:{row.monitor_id}:{kind}:{current.strftime('%Y%m%d%H%M')}",
                    )
                    sent.append(notification.notification_id)
                    kinds.append(kind)
                row.last_alert_at, row.last_alert_types, row.updated_at = (
                    current.astimezone(UTC),
                    kinds,
                    current.astimezone(UTC),
                )
            if sent:
                session.commit()
            return sent
