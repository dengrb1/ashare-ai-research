from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.orchestration import exit_advice_jobs
from ashare_ai.storage.models import Base, ExitAdviceRow, UserAccount, UserAssetState


class _Calendar:
    def sessions(self, start: date, end: date) -> set[date]:
        del end
        return {start}


class _Market:
    def quotes(self, symbols: list[str]) -> list[dict[str, object]]:
        assert symbols == ["600000.SH"]
        return [
            {
                "symbol": "600000.SH",
                "price": 12,
                "status": {"collected_at": "2026-07-20T10:00:00+08:00"},
            }
        ]


def test_intraday_profit_trigger_is_queued_once_until_material_change(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            UserAccount(
                user_id="user-1",
                username="fixture",
                password_hash="hash",
                role="USER",
                enabled=True,
                session_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UserAssetState(
                user_id="user-1",
                watchlist=[],
                positions=[
                    {
                        "symbol": "600000.SH",
                        "name": "浦发银行",
                        "quantity": 1000,
                        "cost": 10,
                        "acquired_on": "2026-07-17",
                    }
                ],
                exit_monitor_enabled=True,
                default_profit_trigger=1000,
                updated_at=now,
            )
        )
        session.commit()

    monkeypatch.setattr(exit_advice_jobs, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(exit_advice_jobs, "FreeExchangeCalendar", lambda: _Calendar())
    monkeypatch.setattr(exit_advice_jobs, "get_market_data_service", lambda: _Market())
    queued: list[str] = []
    current = datetime(2026, 7, 20, 10, tzinfo=exit_advice_jobs.SHANGHAI)

    first = exit_advice_jobs.dispatch_exit_advice(now=current, enqueue=queued.append)
    second = exit_advice_jobs.dispatch_exit_advice(now=current, enqueue=queued.append)

    assert first["state"] == second["state"] == "READY"
    assert len(queued) == 1
    with Session(engine) as session:
        row = session.scalar(select(ExitAdviceRow))
        assert row is not None
        assert row.unrealized_profit == 2000
        assert row.trigger_amount == 1000
        assert row.status == "PENDING"
