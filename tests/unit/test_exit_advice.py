from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.agents.openai_compatible import OpenAICompatibleError
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


def test_position_price_trigger_takes_priority_and_keeps_legacy_equivalent(monkeypatch) -> None:
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
                user_id="price-user",
                username="price-fixture",
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
                user_id="price-user",
                watchlist=[],
                positions=[
                    {
                        "symbol": "600000.SH",
                        "name": "浦发银行",
                        "quantity": 1000,
                        "cost": 10,
                        "acquired_on": "2026-07-17",
                        "profit_trigger_amount": 100,
                        "exit_trigger_price": 11.5,
                    }
                ],
                exit_monitor_enabled=True,
                default_profit_trigger=50,
                updated_at=now,
            )
        )
        session.commit()

    monkeypatch.setattr(exit_advice_jobs, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(exit_advice_jobs, "FreeExchangeCalendar", lambda: _Calendar())
    monkeypatch.setattr(exit_advice_jobs, "get_market_data_service", lambda: _Market())
    queued: list[str] = []

    result = exit_advice_jobs.dispatch_exit_advice(
        now=datetime(2026, 7, 20, 10, tzinfo=exit_advice_jobs.SHANGHAI),
        enqueue=queued.append,
    )

    assert result["state"] == "READY"
    assert len(queued) == 1
    with Session(engine) as session:
        row = session.scalar(select(ExitAdviceRow))
        assert row is not None
        assert row.trigger_type == "PRICE"
        assert row.trigger_price == Decimal("11.500000")
        assert row.trigger_amount == Decimal("1500.00")


def test_exit_worker_persists_stable_model_and_processing_failure_codes(monkeypatch) -> None:
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
                user_id="failure-user",
                username="failure-user",
                password_hash="hash",
                role="USER",
                enabled=True,
                session_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        for advice_id in ("model-failure", "processing-failure"):
            session.add(
                ExitAdviceRow(
                    advice_id=advice_id,
                    user_id="failure-user",
                    symbol="600000.SH",
                    status="PENDING",
                    decision_at=now,
                    available_at=now,
                    current_price=Decimal("10"),
                    unrealized_profit=Decimal("0"),
                    trigger_amount=Decimal("0"),
                    trigger_type="MANUAL",
                    position_snapshot={"symbol": "600000.SH"},
                    research_context={},
                    prompt_version="fixture",
                    input_hash=(advice_id * 8)[:64].ljust(64, "0"),
                    cache_hit=False,
                    created_at=now,
                )
            )
        session.commit()

    monkeypatch.setattr(exit_advice_jobs, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(
        exit_advice_jobs,
        "execute_exit_advice",
        lambda _advice_id: (_ for _ in ()).throw(OpenAICompatibleError("api_key=secret")),
    )
    assert exit_advice_jobs.run_exit_advice_job("model-failure") == {}
    monkeypatch.setattr(
        exit_advice_jobs,
        "execute_exit_advice",
        lambda _advice_id: (_ for _ in ()).throw(RuntimeError("provider details")),
    )
    assert exit_advice_jobs.run_exit_advice_job("processing-failure") == {}

    with Session(engine) as session:
        model_failure = session.get(ExitAdviceRow, "model-failure")
        processing_failure = session.get(ExitAdviceRow, "processing-failure")
        assert model_failure is not None and processing_failure is not None
        assert model_failure.status == "UNAVAILABLE"
        assert model_failure.error_message == "MODEL_UNAVAILABLE"
        assert model_failure.result == {
            "failure_code": "MODEL_UNAVAILABLE",
            "paper_trade_only": True,
        }
        assert processing_failure.status == "FAILED"
        assert processing_failure.error_message == "PROCESSING_FAILED"
        assert processing_failure.result == {
            "failure_code": "PROCESSING_FAILED",
            "paper_trade_only": True,
        }
