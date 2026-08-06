from __future__ import annotations

from types import SimpleNamespace

from ashare_ai.core.energy_saving import DEEP_STANDBY_SECONDS
from ashare_ai.orchestration import exit_advice_worker


class _FakeSession:
    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, *_args: object, **_kwargs: object) -> None:
        return None


def test_exit_worker_executes_claim_in_isolated_child(monkeypatch) -> None:
    events: list[object] = []

    class Heartbeat:
        def __enter__(self) -> None:
            events.append("heartbeat-enter")

        def __exit__(self, *_args: object) -> None:
            events.append("heartbeat-exit")

    class Queue:
        def requeue_expired(self) -> None:
            events.append("requeue")

        def claim(self) -> str:
            events.append("claim")
            return "advice-1"

        def heartbeat(self, advice_id: str) -> Heartbeat:
            assert advice_id == "advice-1"
            return Heartbeat()

        def acknowledge(self, advice_id: str) -> None:
            events.append(("ack", advice_id))

    monkeypatch.setattr(
        exit_advice_worker,
        "get_settings",
        lambda: SimpleNamespace(redis_url="redis://"),
    )
    monkeypatch.setattr(exit_advice_worker, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(exit_advice_worker, "build_exit_advice_queue", lambda _client: Queue())
    monkeypatch.setattr(
        exit_advice_worker,
        "publish_service_heartbeat",
        lambda _client, *, role, energy_saving=False: events.append(
            ("heartbeat", role, energy_saving)
        ),
    )
    monkeypatch.setattr(
        exit_advice_worker,
        "execute_isolated",
        lambda kind, job_id: events.append((kind, job_id)) or 0,
    )
    monkeypatch.setattr("redis.Redis.from_url", lambda *_args, **_kwargs: object())

    exit_advice_worker.run_loop(max_iterations=1)

    assert events == [
        ("heartbeat", "exit-advice-worker", False),
        "requeue",
        "claim",
        "heartbeat-enter",
        ("exit-review", "advice-1"),
        "heartbeat-exit",
        ("ack", "advice-1"),
    ]


def test_exit_worker_skips_polling_in_deep_standby(monkeypatch) -> None:
    sleeps: list[float] = []

    class Queue:
        def requeue_expired(self) -> None:
            raise AssertionError("must not requeue in deep standby")

        def claim(self) -> str:
            raise AssertionError("must not claim in deep standby")

    class FakeTime:
        def monotonic(self) -> float:
            return 0.0

        def sleep(self, seconds: float) -> None:
            sleeps.append(seconds)

    monkeypatch.setattr(
        exit_advice_worker,
        "get_settings",
        lambda: SimpleNamespace(redis_url="redis://"),
    )
    monkeypatch.setattr(
        exit_advice_worker,
        "_energy_saving_standby",
        lambda _client: DEEP_STANDBY_SECONDS,
    )
    monkeypatch.setattr(exit_advice_worker, "build_exit_advice_queue", lambda _client: Queue())
    monkeypatch.setattr(
        exit_advice_worker,
        "publish_service_heartbeat",
        lambda _client, *, role, energy_saving=False: None,
    )
    monkeypatch.setattr(exit_advice_worker, "time", FakeTime())
    monkeypatch.setattr("redis.Redis.from_url", lambda *_args, **_kwargs: object())

    exit_advice_worker.run_loop(max_iterations=3)

    assert sleeps == [DEEP_STANDBY_SECONDS, DEEP_STANDBY_SECONDS]
