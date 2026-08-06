from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.energy_saving import (
    DEEP_STANDBY_SECONDS,
    EnergySavingState,
    evaluate,
    force_wake,
    rearm,
)
from ashare_ai.core.time import SHANGHAI
from ashare_ai.storage.models import BacktestRun, Base, ExitAdviceRow, JobRun, TradePlanRow

AFTER_CLOSE = datetime(2026, 8, 6, 16, 0, tzinfo=SHANGHAI)  # Thursday 16:00 Asia/Shanghai
BEFORE_CLOSE = datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI)  # Thursday 10:00


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def get(self, key: str) -> object:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        del ex
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    return factory()


def _settings(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(energy_saving_enabled=enabled)


def _add_daily(session: Session, *, run_id: str, status: str) -> None:
    session.add(
        JobRun(
            run_id=run_id,
            run_type="DAILY",
            trading_date=date(2026, 8, 6),
            decision_at=datetime(2026, 8, 6, 16, 0, tzinfo=UTC),
            status=status,
            idempotency_key=run_id,
            manifest={},
            input_hash="a" * 64,
            started_at=datetime(2026, 8, 6, 16, 0, tzinfo=UTC),
        )
    )
    session.commit()


def _state(session: Session, **kwargs) -> EnergySavingState:
    return evaluate(redis_client=FakeRedis(), session=session, settings=_settings(), **kwargs)


def test_disabled_switch_keeps_the_system_awake() -> None:
    session = _session()
    state = evaluate(
        redis_client=FakeRedis(),
        session=session,
        settings=_settings(enabled=False),
        now=AFTER_CLOSE,
    )
    assert state.active is False
    assert state.enabled is False
    assert state.reason == "energy saving is not enabled"
    assert state.deep_standby_seconds == 0


def test_market_still_open_blocks_energy_saving() -> None:
    session = _session()
    state = _state(session, now=BEFORE_CLOSE)
    assert state.active is False
    assert state.reason == "market session is still open"


def test_enters_after_close_with_no_active_work() -> None:
    session = _session()
    state = _state(session, now=AFTER_CLOSE)
    assert state.active is True
    assert state.reason == "after close and all daily research complete"
    assert state.deep_standby_seconds == DEEP_STANDBY_SECONDS
    assert state.entered_at is not None


def test_active_research_keeps_the_system_awake() -> None:
    session = _session()
    _add_daily(session, run_id="running-research", status="RUNNING")
    state = _state(session, now=AFTER_CLOSE)
    assert state.active is False
    assert "research run(s) still active" in state.reason


def test_active_backtest_keeps_the_system_awake() -> None:
    session = _session()
    now = datetime.now(UTC)
    session.add(
        BacktestRun(
            backtest_id="running-backtest",
            user_id="u",
            name="running",
            status="RUNNING",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 8, 6),
            config={},
            snapshot_ids=["s"],
            input_hash="b" * 64,
            created_at=now,
        )
    )
    session.commit()
    state = _state(session, now=AFTER_CLOSE)
    assert state.active is False
    assert "backtest run(s) still active" in state.reason


def test_active_trade_plan_keeps_the_system_awake() -> None:
    session = _session()
    now = datetime.now(UTC)
    session.add(
        TradePlanRow(
            plan_id="pending-plan",
            user_id="u",
            report_id="r",
            run_id="job-1",
            trading_date=date(2026, 8, 6),
            decision_at=now,
            available_at=now,
            status="PENDING",
            objective="long_term",
            symbols=["600519.SH"],
            request_payload={},
            snapshot_ids=[],
            optimizer_version="v1",
            config_version="v1",
            input_hash="c" * 64,
            created_at=now,
        )
    )
    session.commit()
    state = _state(session, now=AFTER_CLOSE)
    assert state.active is False
    assert "trade plan(s) still active" in state.reason


def test_active_exit_advice_keeps_the_system_awake() -> None:
    session = _session()
    now = datetime.now(UTC)
    session.add(
        ExitAdviceRow(
            advice_id="pending-advice",
            user_id="u",
            symbol="600519.SH",
            decision_at=now,
            available_at=now,
            current_price="1500.0",
            unrealized_profit="100.0",
            trigger_amount="1500.0",
            position_snapshot={},
            research_context={},
            status="PENDING",
            prompt_version="v1",
            input_hash="d" * 64,
            created_at=now,
        )
    )
    session.commit()
    state = _state(session, now=AFTER_CLOSE)
    assert state.active is False
    assert "exit advice task(s) still active" in state.reason


def test_force_wake_then_rearm() -> None:
    session = _session()
    redis = FakeRedis()
    force_wake(redis)
    state = evaluate(redis_client=redis, session=session, settings=_settings(), now=AFTER_CLOSE)
    assert state.active is False
    assert state.manual_wake is True
    assert state.reason == "administrator forced a wake for this cycle"
    rearm(redis)
    state = evaluate(redis_client=redis, session=session, settings=_settings(), now=AFTER_CLOSE)
    assert state.active is True
    assert state.manual_wake is False


def test_entered_at_is_preserved_across_active_evaluations() -> None:
    session = _session()
    redis = FakeRedis()
    first = evaluate(redis_client=redis, session=session, settings=_settings(), now=AFTER_CLOSE)
    second = evaluate(redis_client=redis, session=session, settings=_settings(), now=AFTER_CLOSE)
    assert first.active is True and second.active is True
    assert second.entered_at == first.entered_at


def test_redis_outage_degrades_to_awake_not_to_standby() -> None:
    session = _session()

    class BrokenRedis:
        def get(self, _key: str) -> object:
            raise RuntimeError("redis down")

        def set(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("redis down")

    state = evaluate(
        redis_client=BrokenRedis(), session=session, settings=_settings(), now=AFTER_CLOSE
    )
    assert state.active is True
    assert state.manual_wake is False
