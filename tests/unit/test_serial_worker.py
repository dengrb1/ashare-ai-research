from __future__ import annotations

from types import SimpleNamespace

from ashare_ai.orchestration import serial_worker


def test_research_queue_uses_its_delayed_queue() -> None:
    queues = serial_worker.build_queues(object())

    research = next(queue for spec, queue in queues if spec.kind == "research")

    assert research.delayed == "ashare:research:delayed"


def test_serial_worker_promotes_due_delayed_jobs_before_claiming(monkeypatch) -> None:
    events: list[str] = []

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
    monkeypatch.setattr(
        serial_worker,
        "AttachmentService",
        lambda _session: SimpleNamespace(cleanup_expired=lambda: 0),
    )
    monkeypatch.setattr(
        serial_worker,
        "NotificationService",
        lambda _session: SimpleNamespace(cleanup_expired=lambda: 0),
    )
    monkeypatch.setattr(serial_worker, "cleanup_expired_archives", lambda: 0)
    monkeypatch.setattr(serial_worker, "execute_isolated", lambda *_args: 0)

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: object())

    serial_worker.run_loop(max_iterations=1)

    assert events == ["promote_due", "requeue_expired", "claim"]
