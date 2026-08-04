from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ashare_ai.core.time import SHANGHAI
from ashare_ai.portfolio.trade_advice import TradeAdviceService
from ashare_ai.storage.models import (
    Base,
    NotificationRow,
    TradeAdviceMonitorRow,
)

TRADING_NOON = datetime(2026, 7, 14, 12, 0, tzinfo=SHANGHAI)


class FakeMarket:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def quotes(self, symbols: list[str], *, force_refresh: bool = False) -> list[dict[str, object]]:
        del force_refresh
        wanted = set(symbols)
        return [item for item in self.rows if item.get("symbol") in wanted]


def _engine_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _monitor(
    factory, *, symbol: str = "600000.SH", ai_buy_price: Decimal | None = None
) -> TradeAdviceMonitorRow:
    with factory() as session:
        now = datetime.now(UTC)
        row = TradeAdviceMonitorRow(
            user_id="user-1",
            symbol=symbol,
            enabled=True,
            ai_buy_price=ai_buy_price,
            ai_sell_price=Decimal("20"),
            rationale={},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def test_generate_daily_treats_zero_price_as_unavailable() -> None:
    factory = _engine_factory()
    monitor = _monitor(factory)
    service = TradeAdviceService(
        market=FakeMarket([{"symbol": monitor.symbol, "price": "0", "change_pct": "0"}]),
        session_factory=factory,
    )

    service.generate_daily(now=TRADING_NOON)

    with factory() as session:
        row = session.get(TradeAdviceMonitorRow, monitor.monitor_id)
        assert row is not None
        assert row.error_code == "QUOTE_UNAVAILABLE"
        # A zero price must never materialise a zero buy target.
        assert row.ai_buy_price is None


def test_dispatch_skips_zero_price_buy_alert() -> None:
    factory = _engine_factory()
    monitor = _monitor(factory, ai_buy_price=Decimal("10"))
    service = TradeAdviceService(
        market=FakeMarket([{"symbol": monitor.symbol, "price": "0", "change_pct": "0"}]),
        session_factory=factory,
    )

    sent = service.dispatch(now=TRADING_NOON)

    assert sent == []
    with factory() as session:
        rows = session.scalars(select(NotificationRow)).all()
        assert rows == []


def test_dispatch_fires_buy_alert_for_positive_price_below_target() -> None:
    factory = _engine_factory()
    monitor = _monitor(factory, ai_buy_price=Decimal("10"))
    service = TradeAdviceService(
        market=FakeMarket([{"symbol": monitor.symbol, "price": "9.5", "change_pct": "1"}]),
        session_factory=factory,
    )

    sent = service.dispatch(now=TRADING_NOON)

    assert len(sent) == 1
    with factory() as session:
        notification = session.scalar(select(NotificationRow))
        assert notification is not None
        assert notification.notification_type == "BUY_TARGET_HIT"


def test_generate_daily_requires_trading_hours() -> None:
    factory = _engine_factory()
    monitor = _monitor(factory)
    service = TradeAdviceService(
        market=FakeMarket([{"symbol": monitor.symbol, "price": "10", "change_pct": "0"}]),
        session_factory=factory,
    )
    before_open = TRADING_NOON.replace(
        hour=9, minute=0, tzinfo=SHANGHAI
    )

    assert service.generate_daily(now=before_open) == 0
    with factory() as session:
        row = session.get(TradeAdviceMonitorRow, monitor.monitor_id)
        assert row is not None and row.error_code is None


def test_generate_daily_materialises_targets_for_positive_price() -> None:
    factory = _engine_factory()
    monitor = _monitor(factory)
    service = TradeAdviceService(
        market=FakeMarket([{"symbol": monitor.symbol, "price": "12", "change_pct": "2"}]),
        session_factory=factory,
    )

    changed = service.generate_daily(now=TRADING_NOON)

    assert changed == 1
    with factory() as session:
        row = session.get(TradeAdviceMonitorRow, monitor.monitor_id)
        assert row is not None
        assert row.error_code is None
        assert row.ai_buy_price is not None and row.ai_buy_price > 0
        assert row.generated_for == TRADING_NOON.astimezone(SHANGHAI).date()
