from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.core.time import SHANGHAI
from ashare_ai.portfolio import monitoring
from ashare_ai.portfolio.monitoring import StopLossMonitorService, derive_stop_loss_price
from ashare_ai.storage.models import (
    Base,
    ExitAdviceRow,
    NotificationRow,
    UserAccount,
    UserAssetState,
)


def _engine():
    return create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


class _Calendar:
    def sessions(self, start: date, end: date) -> set[date]:
        return {
            item
            for item in (date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22))
            if start <= item <= end
        }


class _Market:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def quotes(self, symbols: list[str]) -> list[dict[str, object]]:
        return [
            {
                "symbol": symbol,
                "price": 9.4,
                "status": {"collected_at": self.now.isoformat(), "source": "fixture"},
            }
            for symbol in symbols
        ]

    def klines(self, *_: object, **__: object) -> dict[str, object]:
        return {"bars": []}


def test_derive_stop_loss_clamps_manual_value_and_uses_eight_percent_fallback() -> None:
    clamped = derive_stop_loss_price({"cost": 100, "stop_loss_price": 80})
    fallback = derive_stop_loss_price({"cost": 100}, [])

    assert clamped.mode == "MANUAL"
    assert clamped.price == Decimal("90.00")
    assert clamped.loss_pct == Decimal("0.10")
    assert fallback.mode == "FALLBACK_8PCT"
    assert fallback.price == Decimal("92.00")


def test_stop_loss_notifies_before_paper_exit_research_and_respects_cooldown(monkeypatch) -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 20, 10, tzinfo=SHANGHAI)
    with Session(engine) as session:
        session.add(
            UserAccount(
                user_id="risk-user",
                username="risk-user",
                password_hash="hash",
                role="USER",
                enabled=True,
                session_version=1,
                created_at=now.astimezone(UTC),
                updated_at=now.astimezone(UTC),
            )
        )
        session.add(
            UserAssetState(
                user_id="risk-user",
                watchlist=[],
                positions=[
                    {
                        "symbol": "600000.SH",
                        "name": "浦发银行",
                        "quantity": 100,
                        "cost": 10,
                        "stop_loss_price": 9.5,
                        "stop_loss_enabled": True,
                    }
                ],
                exit_monitor_enabled=False,
                stop_loss_monitor_enabled=True,
                buy_monitor_enabled=True,
                updated_at=now.astimezone(UTC),
            )
        )
        session.commit()

    calls: list[str] = []

    def create_exit(**kwargs: object) -> SimpleNamespace:
        with Session(engine) as session:
            notification = session.scalar(
                select(NotificationRow).where(
                    NotificationRow.user_id == "risk-user",
                    NotificationRow.notification_type == "STOP_LOSS_TRIGGERED",
                )
            )
            assert notification is not None
            session.add(
                ExitAdviceRow(
                    advice_id="cooldown-advice",
                    user_id="risk-user",
                    symbol="600000.SH",
                    status="PENDING",
                    decision_at=now.astimezone(UTC),
                    available_at=now.astimezone(UTC),
                    current_price=Decimal("9.4"),
                    unrealized_profit=Decimal("-60.00"),
                    trigger_amount=Decimal("0"),
                    trigger_type="STOP_LOSS",
                    trigger_price=Decimal("9.5"),
                    position_snapshot={},
                    research_context={},
                    prompt_version="fixture",
                    input_hash="a" * 64,
                    cache_hit=False,
                    created_at=now.astimezone(UTC),
                )
            )
            session.commit()
        calls.append(str(kwargs["symbol"]))
        return SimpleNamespace(advice_id="cooldown-advice")

    monkeypatch.setattr(monitoring, "create_stop_loss_exit_advice", create_exit)
    service = StopLossMonitorService(
        market=_Market(now),
        calendar=_Calendar(),
        session_factory=lambda: Session(engine),
    )

    first = service.dispatch(now=now, enqueue=lambda _: None)
    second = service.dispatch(now=now, enqueue=lambda _: None)

    assert first["queued"] == ["cooldown-advice"]
    assert first["notified"]
    assert second["queued"] == []
    assert calls == ["600000.SH"]
    with Session(engine) as session:
        assert session.scalar(select(NotificationRow).where(NotificationRow.user_id == "risk-user"))


def test_buy_monitor_window_starts_on_formal_plan_next_session_and_honors_policy() -> None:
    calendar = _Calendar()

    assert monitoring._buy_monitor_dates(
        calendar,
        date(2026, 7, 20),
        date(2026, 7, 21),
        date(2026, 7, 22),
        1,
    ) == (date(2026, 7, 21),)
    assert monitoring._buy_monitor_dates(
        calendar,
        date(2026, 7, 20),
        date(2026, 7, 22),
        date(2026, 7, 22),
        1,
    ) is None
