from __future__ import annotations

from types import SimpleNamespace

from ashare_ai.orchestration import exit_advice_worker


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
    monkeypatch.setattr(exit_advice_worker, "build_exit_advice_queue", lambda _client: Queue())
    monkeypatch.setattr(
        exit_advice_worker,
        "publish_service_heartbeat",
        lambda _client, *, role: events.append(("heartbeat", role)),
    )
    monkeypatch.setattr(
        exit_advice_worker,
        "execute_isolated",
        lambda kind, job_id: events.append((kind, job_id)) or 0,
    )
    monkeypatch.setattr("redis.Redis.from_url", lambda *_args, **_kwargs: object())

    exit_advice_worker.run_loop(max_iterations=1)

    assert events == [
        ("heartbeat", "exit-advice-worker"),
        "requeue",
        "claim",
        "heartbeat-enter",
        ("exit-review", "advice-1"),
        "heartbeat-exit",
        ("ack", "advice-1"),
    ]
