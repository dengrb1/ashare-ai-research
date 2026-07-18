from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.hashing import stable_hash
from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration.research_schedule import (
    auto_dispatch_state,
    dispatch_auto_research,
    resolve_manual_research_date,
)
from ashare_ai.orchestration.runner import (
    run_scheduler_loop,
    seconds_until_next_tick,
)
from ashare_ai.storage.models import Base, JobRun, UserAccount, UserResearchPreference


class Calendar:
    def sessions(self, start_date: date, end_date: date) -> tuple[date, ...]:
        del start_date, end_date
        return (date(2026, 7, 14), date(2026, 7, 15))


class Ready:
    def ready(self, trading_date: date, checked_at: datetime) -> bool:
        del checked_at
        return trading_date == date(2026, 7, 15)


class NotReady:
    def ready(self, trading_date: date, checked_at: datetime) -> bool:
        del trading_date, checked_at
        return False


def test_manual_resolver_uses_previous_ready_session_before_close() -> None:
    resolved = resolve_manual_research_date(
        requested_date=date(2026, 7, 15),
        now=datetime(2026, 7, 15, 10, tzinfo=SHANGHAI),
        sessions=(date(2026, 7, 14), date(2026, 7, 15)),
        data_ready=lambda value: value == date(2026, 7, 14),
    )
    assert resolved == date(2026, 7, 14)


def test_auto_dispatch_state_enforces_window_and_readiness() -> None:
    sessions = (date(2026, 7, 15),)
    assert auto_dispatch_state(
        now=datetime(2026, 7, 15, 15, 4, tzinfo=SHANGHAI),
        sessions=sessions,
        data_ready=True,
    ) == "BEFORE_WINDOW"
    assert auto_dispatch_state(
        now=datetime(2026, 7, 15, 15, 10, tzinfo=SHANGHAI),
        sessions=sessions,
        data_ready=False,
    ) == "WAITING_FOR_DATA"
    assert auto_dispatch_state(
        now=datetime(2026, 7, 15, 17, 6, tzinfo=SHANGHAI),
        sessions=sessions,
        data_ready=True,
    ) == "RETRY_EXPIRED"


def test_auto_dispatch_is_per_user_and_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    user_id = str(uuid4())
    now = datetime(2026, 7, 15, 15, 10, tzinfo=SHANGHAI)
    with factory() as session:
        session.add(
            UserAccount(
                user_id=user_id,
                username="auto-user",
                password_hash="hash",
                role="USER",
                enabled=True,
                session_version=1,
                created_at=now.astimezone(UTC),
                updated_at=now.astimezone(UTC),
            )
        )
        session.add(
            UserResearchPreference(
                user_id=user_id,
                auto_enabled=True,
                updated_at=now.astimezone(UTC),
            )
        )
        session.commit()

    class Pipeline:
        def start_run(self, trading_date: date) -> str:
            run_id = str(uuid4())
            manifest = {"policy_version": "first-release-v2"}
            with factory() as session:
                session.add(
                    JobRun(
                        run_id=run_id,
                        run_type="DAILY",
                        trading_date=trading_date,
                        decision_at=now,
                        status="RUNNING",
                        idempotency_key=stable_hash({"run_id": run_id}),
                        manifest=manifest,
                        input_hash=stable_hash(manifest),
                        started_at=now.astimezone(UTC),
                    )
                )
                session.commit()
            return run_id

    queued: list[str] = []
    first = dispatch_auto_research(
        now=now,
        calendar=Calendar(),
        readiness=Ready(),
        session_factory=factory,
        pipeline_factory=Pipeline,
        enqueue=queued.append,
    )
    second = dispatch_auto_research(
        now=now,
        calendar=Calendar(),
        readiness=Ready(),
        session_factory=factory,
        pipeline_factory=Pipeline,
        enqueue=queued.append,
    )

    assert first["enabled_user_count"] == 1
    assert len(queued) == 1
    assert second["queued"] == []
    with factory() as session:
        run = session.scalar(select(JobRun))
        assert run is not None
        assert run.manifest["trigger_source"] == "AUTO"
        assert run.manifest["requested_date"] == "2026-07-15"
        assert run.manifest["actual_research_date"] == "2026-07-15"


def test_auto_dispatch_does_not_create_run_before_data_ready() -> None:
    result = dispatch_auto_research(
        now=datetime(2026, 7, 15, 15, 10, tzinfo=SHANGHAI),
        calendar=Calendar(),
        readiness=NotReady(),
        pipeline_factory=lambda: (_ for _ in ()).throw(AssertionError("must not load pipeline")),
    )
    assert result == {"state": "WAITING_FOR_DATA", "queued": []}


def test_scheduler_aligns_retries_to_shanghai_five_minute_ticks() -> None:
    now = datetime(2026, 7, 15, 15, 4, 30, tzinfo=SHANGHAI)
    assert seconds_until_next_tick(now) == 30

    calls: list[str] = []
    sleeps: list[float] = []
    run_scheduler_loop(
        dispatch=lambda: calls.append("dispatch"),
        now_factory=lambda: now,
        sleep=sleeps.append,
        max_iterations=2,
    )

    assert calls == ["dispatch", "dispatch"]
    assert sleeps == [30]
