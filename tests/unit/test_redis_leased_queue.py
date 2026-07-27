from __future__ import annotations

import pytest

from ashare_ai.orchestration.redis_queue import RedisLeasedQueue


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def lpush(self, key, item):
        self.lists.setdefault(key, []).insert(0, item)

    def zadd(self, key, values):
        self.zsets.setdefault(key, {}).update(values)

    def eval(self, script, number, *args):
        del number
        if "leased-queue-claim" in script:
            pending, processing, leases, owners, now, duration, token = args
            if not self.lists.get(pending):
                return None
            item = self.lists[pending].pop()
            self.lists.setdefault(processing, []).insert(0, item)
            self.zsets.setdefault(leases, {})[item] = float(now) + float(duration)
            self.hashes.setdefault(owners, {})[item] = token
            return item
        if "leased-queue-ack" in script:
            processing, leases, owners, item, token = args
            if self.hashes.setdefault(owners, {}).get(item) != token:
                return 0
            if item in self.lists.get(processing, []):
                self.lists[processing].remove(item)
            self.zsets.setdefault(leases, {}).pop(item, None)
            return int(self.hashes[owners].pop(item, None) is not None)
        if "leased-queue-renew" in script:
            leases, owners, item, token, deadline = args
            if self.hashes.setdefault(owners, {}).get(item) != token:
                return 0
            self.zsets.setdefault(leases, {})[item] = float(deadline)
            return 1
        if "leased-queue-requeue" in script:
            processing, leases, pending, owners, now = args
            expired = [
                item
                for item, deadline in self.zsets.setdefault(leases, {}).items()
                if deadline <= float(now)
            ]
            for item in expired:
                if item in self.lists.get(processing, []):
                    self.lists[processing].remove(item)
                    self.lpush(pending, item)
                self.zsets[leases].pop(item, None)
                self.hashes.setdefault(owners, {}).pop(item, None)
            return expired
        raise AssertionError("unknown script")


def test_only_expired_processing_items_are_recovered() -> None:
    current = [100.0]
    client = FakeRedis()
    queue = RedisLeasedQueue(
        client,
        pending="pending",
        processing="processing",
        lease_seconds=30,
        clock=lambda: current[0],
    )
    queue.enqueue("run-1")
    assert queue.claim() == "run-1"
    assert queue.requeue_expired() == []
    current[0] = 131
    assert queue.requeue_expired() == ["run-1"]
    assert queue.claim() == "run-1"
    queue.acknowledge("run-1")
    assert client.lists["processing"] == []


def test_expired_owner_cannot_renew_or_ack_a_reclaimed_item() -> None:
    current = [100.0]
    client = FakeRedis()
    old = RedisLeasedQueue(
        client,
        pending="pending",
        processing="processing",
        lease_seconds=30,
        clock=lambda: current[0],
    )
    new = RedisLeasedQueue(
        client,
        pending="pending",
        processing="processing",
        lease_seconds=30,
        clock=lambda: current[0],
    )
    old.enqueue("run-1")
    assert old.claim() == "run-1"
    current[0] = 131
    assert new.requeue_expired() == ["run-1"]
    assert new.claim() == "run-1"
    current[0] = 140
    old.renew("run-1")
    old.acknowledge("run-1")
    assert client.lists["processing"] == ["run-1"]
    new.acknowledge("run-1")
    assert client.lists["processing"] == []


def test_failed_item_does_not_stop_the_worker_loop() -> None:
    client = FakeRedis()
    queue = RedisLeasedQueue(
        client,
        pending="pending",
        processing="processing",
        lease_seconds=30,
        clock=lambda: 100.0,
    )
    queue.enqueue("run-1")
    queue.enqueue("run-2")
    handled: list[str] = []
    errors: list[tuple[str, str]] = []

    def handler(item: str) -> None:
        handled.append(item)
        if item == "run-1":
            raise RuntimeError("expected failure")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        queue.consume_forever(
            handler,
            on_error=lambda item, error: errors.append((item, str(error))),
        )
    assert handled == ["run-1", "run-2"]
    assert errors == [("run-1", "expected failure")]
    assert client.lists["processing"] == []

def test_two_independent_consumers_claim_distinct_research_runs() -> None:
    client = FakeRedis()
    consumer_a = RedisLeasedQueue(
        client,
        pending="research:pending",
        processing="research:processing",
        lease_seconds=30,
        clock=lambda: 100.0,
    )
    consumer_b = RedisLeasedQueue(
        client,
        pending="research:pending",
        processing="research:processing",
        lease_seconds=30,
        clock=lambda: 100.0,
    )
    consumer_a.enqueue("run-1")
    consumer_a.enqueue("run-2")

    claimed = {consumer_a.claim(), consumer_b.claim()}
    assert claimed == {"run-1", "run-2"}
    assert consumer_a.claim() is None
