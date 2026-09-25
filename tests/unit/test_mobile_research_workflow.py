from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.hashing import stable_hash
from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration.research_schedule import _submit_auto_for_user
from ashare_ai.storage.models import Base, JobRun
from ashare_ai.trading.sellability import position_sellability


def test_position_sellability_fails_closed_and_marks_t1() -> None:
    trading_date = date(2026, 7, 24)
    today = position_sellability(
        {"quantity": 500, "acquired_on": "2026-07-24"}, trading_date=trading_date
    )
    prior = position_sellability(
        {"quantity": 500, "acquired_on": "2026-07-23"}, trading_date=trading_date
    )
    missing = position_sellability({"quantity": 500}, trading_date=trading_date)

    assert today.t1_restricted and today.sellable_quantity == 0
    assert prior.sellable_quantity == 500 and not prior.blockers
    assert missing.sellable_quantity == 0 and missing.blockers == ("MISSING_ACQUIRED_ON",)


def test_auto_submission_persists_visible_data_readiness_wait() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    submitted_at = datetime(2026, 7, 24, 15, 5, tzinfo=SHANGHAI)

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
                        decision_at=submitted_at.astimezone(UTC),
                        status="RUNNING",
                        idempotency_key=stable_hash({"run_id": run_id}),
                        manifest=manifest,
                        input_hash=stable_hash(manifest),
                        started_at=submitted_at.astimezone(UTC),
                    )
                )
                session.commit()
            return run_id

    delayed: list[tuple[str, datetime]] = []
    run_id = _submit_auto_for_user(
        user_id=str(uuid4()),
        trading_date=submitted_at.date(),
        report={
            "slot": "A",
            "scope": "MARKET",
            "symbols": [],
            "total_budget": 1_000_000,
            "per_symbol_budget": 80_000,
            "max_stock_price": None,
            "config_version": 1,
        },
        pipeline=Pipeline(),
        session_factory=factory,
        enqueue=lambda _: None,
        enqueue_at=lambda item, available_at: delayed.append((item, available_at)),
        data_ready=False,
        submitted_at=submitted_at,
        sessions=(date(2026, 7, 24), date(2026, 7, 27)),
    )

    assert run_id and delayed[0][0] == run_id
    with factory() as session:
        run = session.get(JobRun, run_id)
        assert run is not None and run.status == "DATA_READINESS_WAITING"
        wait = run.manifest["data_readiness_wait"]
        assert wait["next_retry_at"] and wait["deadline_at"]
        assert wait["deadline_at"] == "2026-07-27T01:25:00+00:00"
        assert run.audit_events[0].event_type == "DATA_READINESS_WAITING"
