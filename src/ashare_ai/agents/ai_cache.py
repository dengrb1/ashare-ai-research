"""Shared, user-isolated AI result cache with cross-process single-flight."""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ashare_ai.agents.protocols import StructuredGeneration
from ashare_ai.core.config import get_settings
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import AIResponseCacheRow

logger = logging.getLogger(__name__)

_LOCAL_LEASES: dict[str, threading.Lock] = {}
_LOCAL_LEASES_GUARD = threading.Lock()
_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


@dataclass(frozen=True)
class AICacheGeneration:
    response: dict[str, Any]
    model_name: str
    reasoning_effort: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_policy: str = "COMPATIBLE"

    @classmethod
    def from_structured(cls, generation: StructuredGeneration) -> AICacheGeneration:
        metadata = generation.metadata
        return cls(
            response=dict(generation.output),
            model_name=metadata.model_name,
            reasoning_effort=metadata.reasoning_effort,
            input_tokens=metadata.input_tokens,
            cached_input_tokens=metadata.cached_input_tokens,
            cache_write_tokens=metadata.cache_write_tokens,
            output_tokens=metadata.output_tokens,
            reasoning_tokens=metadata.reasoning_tokens,
            cache_policy=metadata.cache_policy,
        )


@dataclass(frozen=True)
class AICacheResult:
    response: dict[str, Any]
    row: AIResponseCacheRow
    cache_hit: bool
    singleflight_wait_ms: int


@dataclass
class AICacheLease:
    service: AIResultCacheService
    cache_key: str
    owner: str
    wait_ms: int
    _released: bool = False

    def release(self) -> None:
        if not self._released:
            self.service._release(self.cache_key, self.owner)
            self._released = True


class AICacheBusyError(RuntimeError):
    """Raised only when a cache lease remains stuck beyond its bounded wait."""


class AIResultCacheService:
    """Coordinate exact AI result reuse across API and worker processes.

    PostgreSQL remains the source of truth. Redis only leases generation work and
    is allowed to disappear without invalidating a durable cache row.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        redis_client: Any | None = None,
        lease_seconds: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self._redis = redis_client if redis_client is not None else self._build_redis()
        timeout = int(get_settings().llm_timeout_seconds)
        self.lease_seconds = lease_seconds or max(15, min(300, timeout + 10))

    @staticmethod
    def _build_redis() -> Any | None:
        try:
            import redis

            return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
        except Exception:
            return None

    @staticmethod
    def _cache_key(user_id: str, purpose: str, request_sha256: str) -> str:
        return f"{user_id}:{purpose}:{request_sha256}"

    @staticmethod
    def _lease_key(cache_key: str) -> str:
        return f"ashare:ai-cache:lease:v1:{cache_key}"

    def acquire(
        self,
        *,
        user_id: str,
        purpose: str,
        request_sha256: str,
        validate: Callable[[dict[str, Any]], Any],
    ) -> tuple[AICacheResult | None, AICacheLease | None]:
        """Return a validated hit or a lease held until the caller stores a result."""

        started = time.monotonic()
        cache_key = self._cache_key(user_id, purpose, request_sha256)
        cached = self._load(user_id, purpose, request_sha256, validate)
        if cached is not None:
            return (
                self._hit(cached, wait_ms=0, purpose=purpose, request_sha256=request_sha256),
                None,
            )

        owner = self._claim(cache_key)
        wait_deadline = time.monotonic() + self.lease_seconds
        while not owner:
            cached = self._load(user_id, purpose, request_sha256, validate)
            if cached is not None:
                wait_ms = round((time.monotonic() - started) * 1000)
                return (
                    self._hit(
                        cached,
                        wait_ms=wait_ms,
                        purpose=purpose,
                        request_sha256=request_sha256,
                    ),
                    None,
                )
            if time.monotonic() >= wait_deadline:
                owner = self._claim(cache_key)
                if owner:
                    break
                raise AICacheBusyError("AI cache generation is still in progress")
            time.sleep(0.05)
        return None, AICacheLease(
            service=self,
            cache_key=cache_key,
            owner=owner,
            wait_ms=round((time.monotonic() - started) * 1000),
        )

    def store(
        self,
        *,
        lease: AICacheLease,
        user_id: str,
        purpose: str,
        request_sha256: str,
        prompt_version: str,
        ttl_seconds: int,
        validate: Callable[[dict[str, Any]], Any],
        generation: AICacheGeneration,
    ) -> AICacheResult:
        """Validate and persist a result, releasing the lease exactly once."""

        try:
            if not isinstance(generation.response, dict):
                raise ValueError("AI cache response must be an object")
            validate(generation.response)
            row, created = self._store(
                user_id=user_id,
                purpose=purpose,
                request_sha256=request_sha256,
                prompt_version=prompt_version,
                ttl_seconds=ttl_seconds,
                generation=generation,
            )
            if not created:
                return self._hit(
                    row,
                    wait_ms=lease.wait_ms,
                    purpose=purpose,
                    request_sha256=request_sha256,
                )
            logger.info(
                "ai_cache miss",
                extra={
                    "cache_hit": False,
                    "supplier_cache_hit": generation.cached_input_tokens > 0,
                    "supplier_prompt_cache_hit": generation.cached_input_tokens > 0,
                    "cache_layer": (
                        "SUPPLIER_PROMPT" if generation.cached_input_tokens > 0 else "MISS"
                    ),
                    "hit_count": row.hit_count,
                    "singleflight_wait_ms": lease.wait_ms,
                    "purpose": purpose,
                    "request_sha256": request_sha256,
                },
            )
            return AICacheResult(
                response=dict(row.response),
                row=row,
                cache_hit=False,
                singleflight_wait_ms=lease.wait_ms,
            )
        finally:
            lease.release()

    def get_or_generate(
        self,
        *,
        user_id: str,
        purpose: str,
        request_sha256: str,
        prompt_version: str,
        ttl_seconds: int,
        validate: Callable[[dict[str, Any]], Any],
        generate: Callable[[], AICacheGeneration],
    ) -> AICacheResult:
        started = time.monotonic()
        cache_key = self._cache_key(user_id, purpose, request_sha256)
        cached = self._load(user_id, purpose, request_sha256, validate)
        if cached is not None:
            return self._hit(cached, wait_ms=0, purpose=purpose, request_sha256=request_sha256)

        owner = self._claim(cache_key)
        wait_deadline = time.monotonic() + self.lease_seconds
        while not owner:
            cached = self._load(user_id, purpose, request_sha256, validate)
            if cached is not None:
                wait_ms = round((time.monotonic() - started) * 1000)
                return self._hit(
                    cached,
                    wait_ms=wait_ms,
                    purpose=purpose,
                    request_sha256=request_sha256,
                )
            if time.monotonic() >= wait_deadline:
                owner = self._claim(cache_key)
                if owner:
                    break
                raise AICacheBusyError("AI cache generation is still in progress")
            time.sleep(0.05)

        wait_ms = round((time.monotonic() - started) * 1000)
        try:
            generation = generate()
            if not isinstance(generation.response, dict):
                raise ValueError("AI cache response must be an object")
            validate(generation.response)
            row, created = self._store(
                user_id=user_id,
                purpose=purpose,
                request_sha256=request_sha256,
                prompt_version=prompt_version,
                ttl_seconds=ttl_seconds,
                generation=generation,
            )
            if not created:
                return self._hit(
                    row,
                    wait_ms=wait_ms,
                    purpose=purpose,
                    request_sha256=request_sha256,
                )
            logger.info(
                "ai_cache miss",
                extra={
                    "cache_hit": False,
                    "supplier_cache_hit": generation.cached_input_tokens > 0,
                    "supplier_prompt_cache_hit": generation.cached_input_tokens > 0,
                    "cache_layer": (
                        "SUPPLIER_PROMPT" if generation.cached_input_tokens > 0 else "MISS"
                    ),
                    "hit_count": row.hit_count,
                    "singleflight_wait_ms": wait_ms,
                    "purpose": purpose,
                    "request_sha256": request_sha256,
                },
            )
            return AICacheResult(
                response=dict(row.response),
                row=row,
                cache_hit=False,
                singleflight_wait_ms=wait_ms,
            )
        finally:
            self._release(cache_key, owner)

    def invalidate(self, *, user_id: str, purpose: str, request_sha256: str) -> None:
        with self.session_factory() as session:
            row = session.scalar(
                select(AIResponseCacheRow).where(
                    AIResponseCacheRow.user_id == user_id,
                    AIResponseCacheRow.purpose == purpose,
                    AIResponseCacheRow.request_sha256 == request_sha256,
                )
            )
            if row is not None:
                session.delete(row)
                session.commit()

    def _load(
        self,
        user_id: str,
        purpose: str,
        request_sha256: str,
        validate: Callable[[dict[str, Any]], Any],
    ) -> AIResponseCacheRow | None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            row = session.scalar(
                select(AIResponseCacheRow).where(
                    AIResponseCacheRow.user_id == user_id,
                    AIResponseCacheRow.purpose == purpose,
                    AIResponseCacheRow.request_sha256 == request_sha256,
                )
            )
            if row is None:
                return None
            try:
                if _as_utc(row.expires_at) <= now:
                    session.delete(row)
                    session.commit()
                    return None
                validate(dict(row.response))
            except Exception:
                # A malformed or stale row must never poison future generations.
                session.rollback()
                stale = session.get(AIResponseCacheRow, row.cache_id)
                if stale is not None:
                    session.delete(stale)
                    session.commit()
                return None
            return _copy_cache_row(row)

    def _hit(
        self,
        row: AIResponseCacheRow,
        *,
        wait_ms: int,
        purpose: str,
        request_sha256: str,
    ) -> AICacheResult:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            current = session.get(AIResponseCacheRow, row.cache_id)
            if current is not None:
                current.hit_count += 1
                current.last_hit_at = now
                current.last_singleflight_wait_ms = max(0, int(wait_ms))
                session.commit()
                row = _copy_cache_row(current)
        logger.info(
            "ai_cache local hit",
            extra={
                "cache_hit": True,
                "cache_layer": "LOCAL",
                "hit_count": row.hit_count,
                "singleflight_wait_ms": max(0, int(wait_ms)),
                "purpose": purpose,
                "request_sha256": request_sha256,
            },
        )
        return AICacheResult(
            response=dict(row.response),
            row=row,
            cache_hit=True,
            singleflight_wait_ms=max(0, int(wait_ms)),
        )

    def _store(
        self,
        *,
        user_id: str,
        purpose: str,
        request_sha256: str,
        prompt_version: str,
        ttl_seconds: int,
        generation: AICacheGeneration,
    ) -> tuple[AIResponseCacheRow, bool]:
        now = datetime.now(UTC)
        response_sha = _stable_response_hash(generation.response)
        with self.session_factory() as session:
            row = AIResponseCacheRow(
                user_id=user_id,
                purpose=purpose,
                request_sha256=request_sha256,
                response_sha256=response_sha,
                model_name=generation.model_name,
                reasoning_effort=generation.reasoning_effort,
                prompt_version=prompt_version,
                response=dict(generation.response),
                input_tokens=max(0, int(generation.input_tokens)),
                cached_input_tokens=max(0, int(generation.cached_input_tokens)),
                cache_write_tokens=max(0, int(generation.cache_write_tokens)),
                output_tokens=max(0, int(generation.output_tokens)),
                reasoning_tokens=max(0, int(generation.reasoning_tokens)),
                cache_policy=generation.cache_policy,
                hit_count=0,
                created_at=now,
                expires_at=now + timedelta(seconds=max(1, int(ttl_seconds))),
            )
            session.add(row)
            try:
                session.commit()
                return _copy_cache_row(row), True
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(AIResponseCacheRow).where(
                        AIResponseCacheRow.user_id == user_id,
                        AIResponseCacheRow.purpose == purpose,
                        AIResponseCacheRow.request_sha256 == request_sha256,
                    )
                )
                if existing is None:
                    raise
                return _copy_cache_row(existing), False

    def _claim(self, cache_key: str) -> str | None:
        token = secrets.token_urlsafe(18)
        lease_key = self._lease_key(cache_key)
        if self._redis is not None:
            try:
                if bool(self._redis.set(lease_key, token, nx=True, ex=self.lease_seconds)):
                    return f"redis:{token}"
                return None
            except Exception:
                # Redis outage degrades to an in-process lease; PostgreSQL's
                # unique constraint still protects the durable cache row.
                pass
        with _LOCAL_LEASES_GUARD:
            lock = _LOCAL_LEASES.setdefault(cache_key, threading.Lock())
        return f"local:{id(lock)}" if lock.acquire(blocking=False) else None

    def _release(self, cache_key: str, owner: str | None) -> None:
        if not owner:
            return
        token = owner.split(":", 1)[1]
        lease_key = self._lease_key(cache_key)
        if owner.startswith("redis:") and self._redis is not None:
            try:
                self._redis.eval(_RELEASE_SCRIPT, 1, lease_key, token)
            except Exception:
                with suppress(Exception):
                    if self._redis.get(lease_key) == token:
                        self._redis.delete(lease_key)
            return
        with _LOCAL_LEASES_GUARD:
            lock = _LOCAL_LEASES.get(cache_key)
        if lock is not None and str(id(lock)) == token and lock.locked():
            lock.release()


def _copy_cache_row(row: AIResponseCacheRow) -> AIResponseCacheRow:
    """Detach the fields used by callers before closing the cache session."""

    copy = AIResponseCacheRow(
        cache_id=row.cache_id,
        user_id=row.user_id,
        purpose=row.purpose,
        request_sha256=row.request_sha256,
        response_sha256=row.response_sha256,
        model_name=row.model_name,
        reasoning_effort=row.reasoning_effort,
        prompt_version=row.prompt_version,
        response=dict(row.response),
        input_tokens=row.input_tokens,
        cached_input_tokens=row.cached_input_tokens,
        cache_write_tokens=row.cache_write_tokens,
        output_tokens=row.output_tokens,
        reasoning_tokens=row.reasoning_tokens,
        cache_policy=row.cache_policy,
        hit_count=row.hit_count,
        last_singleflight_wait_ms=row.last_singleflight_wait_ms,
        created_at=row.created_at,
        last_hit_at=row.last_hit_at,
        expires_at=row.expires_at,
    )
    return copy


def _stable_response_hash(response: dict[str, Any]) -> str:
    import hashlib

    payload = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """Normalize database datetimes from dialects that drop timezone metadata."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
