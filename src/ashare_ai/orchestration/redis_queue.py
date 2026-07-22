from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

CLAIM_SCRIPT = """
-- leased-queue-claim
local item = redis.call('rpop', KEYS[1])
if item then
  redis.call('lpush', KEYS[2], item)
  redis.call('zadd', KEYS[3], ARGV[1] + ARGV[2], item)
  redis.call('hset', KEYS[4], item, ARGV[3])
end
return item
"""

ACK_SCRIPT = """
-- leased-queue-ack
if redis.call('hget', KEYS[3], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('lrem', KEYS[1], 1, ARGV[1])
redis.call('zrem', KEYS[2], ARGV[1])
return redis.call('hdel', KEYS[3], ARGV[1])
"""

RENEW_SCRIPT = """
-- leased-queue-renew
if redis.call('hget', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
return redis.call('zadd', KEYS[1], ARGV[3], ARGV[1])
"""

REQUEUE_SCRIPT = """
-- leased-queue-requeue
local expired = redis.call('zrangebyscore', KEYS[2], '-inf', ARGV[1])
for _, item in ipairs(expired) do
  if redis.call('lrem', KEYS[1], 1, item) > 0 then
    redis.call('lpush', KEYS[3], item)
  end
  redis.call('zrem', KEYS[2], item)
  redis.call('hdel', KEYS[4], item)
end
return expired
"""

PROMOTE_DELAYED_SCRIPT = """
-- delayed-queue-promote
local items = redis.call('zrangebyscore', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, item in ipairs(items) do
  if redis.call('zrem', KEYS[1], item) > 0 then
    redis.call('lpush', KEYS[2], item)
  end
end
return items
"""


class RedisLeasedQueue:
    def __init__(
        self,
        client: Any,
        *,
        pending: str,
        processing: str,
        lease_seconds: int,
        delayed: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.pending = pending
        self.processing = processing
        self.leases = f"{processing}:leases"
        self.owners = f"{processing}:owners"
        self.delayed = delayed
        self.lease_seconds = lease_seconds
        self.clock = clock
        self._tokens: dict[str, str] = {}

    def enqueue(self, item: str) -> None:
        self.client.lpush(self.pending, item)

    def enqueue_at(self, item: str, available_at: float) -> None:
        if self.delayed is None:
            raise RuntimeError("delayed queue is not configured")
        # ZSET members are unique, so repeated scheduling updates one durable
        # delivery instead of creating duplicate external work.
        self.client.zadd(self.delayed, {item: available_at})

    def promote_due(self, *, limit: int = 100) -> list[str]:
        if self.delayed is None:
            return []
        return cast(
            list[str],
            self.client.eval(
                PROMOTE_DELAYED_SCRIPT,
                2,
                self.delayed,
                self.pending,
                self.clock(),
                limit,
            ),
        )

    def requeue_expired(self) -> list[str]:
        return cast(
            list[str],
            self.client.eval(
                REQUEUE_SCRIPT,
                4,
                self.processing,
                self.leases,
                self.pending,
                self.owners,
                self.clock(),
            ),
        )

    def claim(self) -> str | None:
        token = secrets.token_urlsafe(18)
        item = cast(
            str | None,
            self.client.eval(
                CLAIM_SCRIPT,
                4,
                self.pending,
                self.processing,
                self.leases,
                self.owners,
                self.clock(),
                self.lease_seconds,
                token,
            ),
        )
        if item is not None:
            self._tokens[item] = token
        return item

    def renew(self, item: str) -> None:
        token = self._tokens.get(item)
        if token is None:
            return
        self.client.eval(
            RENEW_SCRIPT,
            2,
            self.leases,
            self.owners,
            item,
            token,
            self.clock() + self.lease_seconds,
        )

    def acknowledge(self, item: str) -> None:
        token = self._tokens.pop(item, None)
        if token is None:
            return
        self.client.eval(
            ACK_SCRIPT,
            3,
            self.processing,
            self.leases,
            self.owners,
            item,
            token,
        )

    @contextmanager
    def heartbeat(self, item: str) -> Iterator[None]:
        stopped = threading.Event()

        def renew() -> None:
            interval = max(1.0, self.lease_seconds / 3)
            while not stopped.wait(interval):
                self.renew(item)

        thread = threading.Thread(target=renew, name="redis-queue-lease", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)

    def consume_forever(
        self,
        handler: Callable[[str], Any],
        *,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        while True:
            self.promote_due()
            self.requeue_expired()
            item = self.claim()
            if item is None:
                time.sleep(1)
                continue
            try:
                with self.heartbeat(item):
                    handler(item)
            except Exception as exc:
                if on_error is not None:
                    on_error(item, exc)
            finally:
                self.acknowledge(item)
