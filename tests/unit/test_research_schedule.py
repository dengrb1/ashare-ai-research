from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.core.hashing import stable_hash
from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration.research_schedule import (
    _READINESS_RESULTS,
    AKShareDataReadiness,
    _automatic_run_key,
    auto_dispatch_state,
    data_readiness_wait,
    dispatch_auto_research,
    resolve_manual_research_date,
)
from ashare_ai.orchestration.runner import (
    run_scheduler_loop,
    seconds_until_next_tick,
)
from ashare_ai.storage.models import (
    AutomaticResearchReportConfig,
    Base,
    JobRun,
    UserAccount,
    UserResearchPreference,
)


class Calendar:
    def sessions(self, start_date: date, end_date: date) -> tuple[date, ...]:
        del start_date, end_date
        return (date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16))


class Ready:
    def ready(self, trading_date: date, checked_at: datetime) -> bool:
        del checked_at
        return trading_date == date(2026, 7, 15)


class NotReady:
    def ready(self, trading_date: date, checked_at: datetime) -> bool:
        del trading_date, checked_at
        return False


def test_manual_resolver_allows_latest_completed_session_during_market_open() -> None:
    assert resolve_manual_research_date(
        requested_date=date(2026, 7, 14),
        now=datetime(2026, 7, 15, 10, tzinfo=SHANGHAI),
        sessions=(date(2026, 7, 14), date(2026, 7, 15)),
        data_ready=lambda value: value == date(2026, 7, 14),
    ) == date(2026, 7, 14)


def test_manual_resolver_rejects_current_session_during_market_open() -> None:
    with pytest.raises(RuntimeError, match="unsafe during the trading session"):
        resolve_manual_research_date(
            requested_date=date(2026, 7, 15),
            now=datetime(2026, 7, 15, 10, tzinfo=SHANGHAI),
            sessions=(date(2026, 7, 14), date(2026, 7, 15)),
            data_ready=lambda value: value == date(2026, 7, 14),
        )


def test_manual_resolver_allows_latest_session_before_open_and_on_weekend() -> None:
    sessions = (date(2026, 7, 17), date(2026, 7, 20))
    assert resolve_manual_research_date(
        requested_date=date(2026, 7, 20),
        now=datetime(2026, 7, 20, 8, 30, tzinfo=SHANGHAI),
        sessions=sessions,
        data_ready=lambda value: value == date(2026, 7, 17),
    ) == date(2026, 7, 17)
    assert resolve_manual_research_date(
        requested_date=date(2026, 7, 19),
        now=datetime(2026, 7, 19, 16, tzinfo=SHANGHAI),
        sessions=(date(2026, 7, 17),),
        data_ready=lambda value: value == date(2026, 7, 17),
    ) == date(2026, 7, 17)


def test_manual_resolver_does_not_fallback_when_post_close_data_is_not_ready() -> None:
    with pytest.raises(RuntimeError, match="selected trading session is not ready"):
        resolve_manual_research_date(
            requested_date=date(2026, 7, 20),
            now=datetime(2026, 7, 20, 15, 56, tzinfo=SHANGHAI),
            sessions=(date(2026, 7, 17), date(2026, 7, 20)),
            data_ready=lambda value: value == date(2026, 7, 17),
        )


def test_auto_dispatch_state_waits_for_late_after_close_data() -> None:
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
    ) == "READY"


def test_data_readiness_wait_uses_authoritative_next_session_cutoff() -> None:
    wait = data_readiness_wait(
        trading_date=date(2026, 7, 17),
        now=datetime(2026, 7, 17, 17, 10, tzinfo=SHANGHAI),
        sessions=(date(2026, 7, 17), date(2026, 7, 20)),
        retry_minutes=5,
    )

    assert wait == {
        "deadline_at": "2026-07-20T01:25:00+00:00",
        "next_retry_at": "2026-07-17T09:15:00+00:00",
        "attempt_count": 0,
    }


def test_data_readiness_wait_rejects_a_calendar_without_next_session() -> None:
    with pytest.raises(RuntimeError, match="next session"):
        data_readiness_wait(
            trading_date=date(2026, 7, 17),
            now=datetime(2026, 7, 17, 17, 10, tzinfo=SHANGHAI),
            sessions=(date(2026, 7, 17),),
            retry_minutes=5,
        )


def test_slot_a_keeps_legacy_keys_during_upgrade() -> None:
    trading_date = date(2026, 7, 15)
    legacy_idempotency = stable_hash(
        {
            "kind": "AUTO_DAILY_RESEARCH",
            "user_id": "user-1",
            "trading_date": trading_date,
        }
    )
    assert _automatic_run_key(
        kind="AUTO_DAILY_RESEARCH",
        user_id="user-1",
        trading_date=trading_date,
        slot="A",
    ) == legacy_idempotency
    assert _automatic_run_key(
        kind="AUTO_DAILY_RESEARCH",
        user_id="user-1",
        trading_date=trading_date,
        slot="B",
    ) != legacy_idempotency


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
                # The normalized report rows are authoritative; this legacy
                # cache may temporarily drift after an interrupted import.
                auto_enabled=False,
                updated_at=now.astimezone(UTC),
            )
        )
        session.add_all(
            [
                AutomaticResearchReportConfig(
                    user_id=user_id,
                    slot=slot,
                    enabled=True,
                    scope="MARKET",
                    symbols=[],
                    total_budget=1_000_000,
                    per_symbol_budget=80_000,
                    max_stock_price=500,
                    config_version=1,
                    updated_at=now.astimezone(UTC),
                )
                for slot in ("A", "B")
            ]
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
    with factory() as session:
        for run in session.scalars(select(JobRun)).all():
            run.status = "SUCCEEDED"
            run.active_research_key = None
        for config in session.scalars(select(AutomaticResearchReportConfig)).all():
            config.config_version += 1
            config.total_budget += 1
        session.commit()
    second = dispatch_auto_research(
        now=now,
        calendar=Calendar(),
        readiness=Ready(),
        session_factory=factory,
        pipeline_factory=Pipeline,
        enqueue=queued.append,
    )

    assert first["enabled_user_count"] == 1
    assert len(queued) == 2
    assert second["queued"] == []
    with factory() as session:
        runs = list(session.scalars(select(JobRun)).all())
        assert len(runs) == 2
        assert {run.manifest["automatic_report_slot"] for run in runs} == {"A", "B"}
        assert all(run.manifest["trigger_source"] == "AUTO" for run in runs)
        assert all(run.manifest["requested_date"] == "2026-07-15" for run in runs)
        assert all(run.manifest["actual_research_date"] == "2026-07-15" for run in runs)
        assert all(
            Decimal(run.manifest["research_budget"]["max_stock_price"]) == Decimal("500")
            for run in runs
        )


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


@pytest.fixture(autouse=True)
def _clean_readiness_cache() -> None:
    """The probe-result cache is a module-level dict shared by every test."""
    _READINESS_RESULTS.clear()
    yield
    _READINESS_RESULTS.clear()


class CountingBenchmarkProvider:
    """Ready immediately: every benchmark series has a bar dated on the end date."""

    def __init__(self) -> None:
        self.calls = 0

    def benchmark_bars_many(self, codes: tuple[str, ...], start_date: date, end_date: date):
        del start_date
        self.calls += 1
        return {code: [{"date": end_date}] for code in codes}


class SlowBenchmarkProvider:
    """Stalls past the probe budget so the wall-clock timeout must cut it off."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def benchmark_bars_many(self, codes: tuple[str, ...], start_date: date, end_date: date):
        del start_date
        time.sleep(self.delay)
        return {code: [{"date": end_date}] for code in codes}


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setattr(
        "ashare_ai.orchestration.akshare_bundle.AKShareCanonicalProvider",
        lambda: provider,
    )


def test_readiness_probes_three_series_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = CountingBenchmarkProvider()
    _patch_provider(monkeypatch, provider)
    readiness = AKShareDataReadiness()
    trading_date = date(2026, 7, 15)
    checked_at = datetime.now(UTC)

    assert readiness.ready(trading_date, checked_at) is True
    assert provider.calls == 1

    # A second probe for the same trading date within the TTL is a cache hit.
    assert readiness.ready(trading_date, checked_at) is True
    assert provider.calls == 1


def test_readiness_cache_expires_after_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = CountingBenchmarkProvider()
    _patch_provider(monkeypatch, provider)
    readiness = AKShareDataReadiness()
    trading_date = date(2026, 7, 15)
    checked_at = datetime.now(UTC)

    assert readiness.ready(trading_date, checked_at) is True
    assert provider.calls == 1

    # A checked_at past the four-minute TTL misses the cache and probes again.
    stale = checked_at + timedelta(seconds=241)
    assert readiness.ready(trading_date, stale) is True
    assert provider.calls == 2


def test_readiness_budget_bounds_a_stalling_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SlowBenchmarkProvider(delay=0.5)
    _patch_provider(monkeypatch, provider)
    readiness = AKShareDataReadiness()
    trading_date = date(2026, 7, 16)
    checked_at = datetime.now(UTC)

    began = time.monotonic()
    assert readiness.ready(trading_date, checked_at, budget_seconds=0.2) is False
    assert time.monotonic() - began < 1.0

    # The probe thread keeps running to completion in the background; drain the
    # single-worker executor so it cannot delay a later test's probe.
    time.sleep(0.6)
