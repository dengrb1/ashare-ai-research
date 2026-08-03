from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.agents.ai_cache import AICacheGeneration, AIResultCacheService
from ashare_ai.storage.models import AIResponseCacheRow, Base, UserAccount


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock = threading.Lock()

    def set(self, key: str, value: str, *, nx: bool = False, ex: int = 0) -> bool:
        del ex
        with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def get(self, key: str) -> str | None:
        with self.lock:
            return self.values.get(key)

    def delete(self, key: str) -> None:
        with self.lock:
            self.values.pop(key, None)

    def eval(self, _script: str, _count: int, key: str, token: str) -> int:
        with self.lock:
            if self.values.get(key) != token:
                return 0
            del self.values[key]
            return 1


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{(tmp_path / 'cache.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            UserAccount(
                user_id="cache-user",
                username="cache-user",
                password_hash="fixture",
                role="USER",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return factory


def test_ai_cache_reuses_result_and_records_hit_count(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    service = AIResultCacheService(session_factory=factory, redis_client=_FakeRedis())
    calls = 0

    def generate() -> AICacheGeneration:
        nonlocal calls
        calls += 1
        return AICacheGeneration(
            response={"value": "stable"},
            model_name="fixture-model",
            reasoning_effort="low",
            input_tokens=10,
            output_tokens=2,
        )

    kwargs = {
        "user_id": "cache-user",
        "purpose": "EXIT_ADVICE",
        "request_sha256": "a" * 64,
        "prompt_version": "fixture-v1",
        "ttl_seconds": 60,
        "validate": lambda value: value["value"],
        "generate": generate,
    }
    first = service.get_or_generate(**kwargs)
    second = service.get_or_generate(**kwargs)

    assert calls == 1
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.response == first.response == {"value": "stable"}
    with factory() as session:
        row = session.scalar(select(AIResponseCacheRow))
        assert row is not None
        assert row.hit_count == 1
        assert row.last_singleflight_wait_ms == 0


def test_ai_cache_single_flight_only_generates_once(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    service = AIResultCacheService(session_factory=factory, redis_client=_FakeRedis())
    calls = 0
    calls_lock = threading.Lock()

    def generate() -> AICacheGeneration:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return AICacheGeneration(
            response={"value": "single"},
            model_name="fixture-model",
            reasoning_effort="low",
        )

    kwargs = {
        "user_id": "cache-user",
        "purpose": "CHAT",
        "request_sha256": "b" * 64,
        "prompt_version": "fixture-v1",
        "ttl_seconds": 60,
        "validate": lambda value: value["value"],
        "generate": generate,
    }

    with __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.get_or_generate(**kwargs), range(2)))

    assert calls == 1
    assert sorted(result.cache_hit for result in results) == [False, True]
    assert max(result.singleflight_wait_ms for result in results) >= 50


def test_invalid_ai_cache_row_is_deleted_before_regeneration(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            AIResponseCacheRow(
                user_id="cache-user",
                purpose="TRADE_PLAN",
                request_sha256="c" * 64,
                response_sha256="d" * 64,
                model_name="fixture-model",
                reasoning_effort="low",
                prompt_version="fixture-v1",
                response={"invalid": True},
                created_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )
        session.commit()

    service = AIResultCacheService(session_factory=factory, redis_client=_FakeRedis())
    result = service.get_or_generate(
        user_id="cache-user",
        purpose="TRADE_PLAN",
        request_sha256="c" * 64,
        prompt_version="fixture-v1",
        ttl_seconds=60,
        validate=lambda value: value["value"],
        generate=lambda: AICacheGeneration(
            response={"value": "replaced"},
            model_name="fixture-model",
            reasoning_effort="low",
        ),
    )

    assert result.cache_hit is False
    assert result.response == {"value": "replaced"}
