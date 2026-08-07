from __future__ import annotations

from types import SimpleNamespace

from pytest import MonkeyPatch

from ashare_ai.core.energy_saving import DEEP_STANDBY_SECONDS
from ashare_ai.orchestration import serial_worker


def test_research_queue_uses_its_delayed_queue() -> None:
    queues = serial_worker.build_queues(object())

    research = next(queue for spec, queue in queues if spec.kind == "research")

    assert research.delayed == "ashare:research:delayed"


def test_dual_job_worker_does_not_build_a_research_consumer() -> None:
    queues = serial_worker.build_queues(object(), execution_mode="DUAL")

    assert [spec.kind for spec, _queue in queues] == [
        "personal-archive",
        "trade-plan",
        "backtest",
    ]


def test_serial_worker_promotes_due_delayed_jobs_before_claiming(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    isolated: list[tuple[str, str]] = []

    class Queue:
        def promote_due(self) -> list[str]:
            events.append("promote_due")
            return ["waiting-run"]

        def requeue_expired(self) -> list[str]:
            events.append("requeue_expired")
            return []

        def claim(self) -> None:
            events.append("claim")
            return None

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def commit(self) -> None:
            return None

    queue = Queue()
    monkeypatch.setattr(
        serial_worker,
        "build_queues",
        lambda _client: [(serial_worker.QUEUE_SPECS[1], queue)],
    )
    monkeypatch.setattr(serial_worker, "get_settings", lambda: SimpleNamespace(redis_url="redis://"))
    monkeypatch.setattr(serial_worker, "SessionLocal", lambda: Session())
    monkeypatch.setattr(serial_worker, "_energy_saving_standby", lambda _client: 0)
    monkeypatch.setattr(
        serial_worker,
        "execute_isolated",
        lambda kind, job_id: isolated.append((kind, job_id)) or 0,
    )

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: object())

    serial_worker.run_loop(max_iterations=1)

    assert events == ["promote_due", "requeue_expired", "claim"]
    assert isolated == [("maintenance", "tick"), ("schedule", "tick")]


def test_serial_worker_skips_polling_in_deep_standby(monkeypatch: MonkeyPatch) -> None:
    sleeps: list[float] = []
    isolated: list[tuple[str, str]] = []
    reclaimed: list[tuple[str, bool]] = []
    runtime = SimpleNamespace(
        execution_mode="SERIAL",
        topology_sha256="t" * 64,
        config_sha256="c" * 64,
        settings=SimpleNamespace(redis_url="redis://", worker_lease_seconds=900),
    )

    class Queue:
        def promote_due(self) -> None:
            raise AssertionError("must not poll in deep standby")

        def claim(self) -> None:
            raise AssertionError("must not claim in deep standby")

    class FakeTime:
        def monotonic(self) -> float:
            return 0.0

        def sleep(self, seconds: float) -> None:
            sleeps.append(seconds)

    monkeypatch.setattr(serial_worker, "_load_worker_runtime", lambda: runtime)
    monkeypatch.setattr(
        serial_worker, "_energy_saving_standby", lambda _client: DEEP_STANDBY_SECONDS
    )
    monkeypatch.setattr(serial_worker, "publish_heartbeat", lambda *_args, **_kwargs: None)
    def reclaim_runtime_memory(
        _settings: object, *, reason: str, force: bool = False
    ) -> None:
        reclaimed.append((reason, force))

    monkeypatch.setattr(serial_worker, "reclaim_runtime_memory", reclaim_runtime_memory)
    monkeypatch.setattr(
        serial_worker,
        "build_queues",
        lambda _client, **_kwargs: [(serial_worker.QUEUE_SPECS[1], Queue())],
    )
    monkeypatch.setattr(
        serial_worker,
        "execute_isolated",
        lambda kind, job_id: isolated.append((kind, job_id)) or 0,
    )
    monkeypatch.setattr(serial_worker, "time", FakeTime())

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: object())

    serial_worker.run_loop(max_iterations=3)

    # Deep standby still runs maintenance + the scheduler (so the next day's
    # research dispatch is never lost) but stops per-second queue polling.
    assert isolated == [("maintenance", "tick"), ("schedule", "tick")]
    assert sleeps == [DEEP_STANDBY_SECONDS, DEEP_STANDBY_SECONDS]
    assert reclaimed == [("job-worker-energy-standby", True)]
