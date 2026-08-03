from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from redis import Redis
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.agents.ai_cache import AICacheGeneration, AIResultCacheService
from ashare_ai.storage.models import AIResponseCacheRow, UserAccount


@pytest.mark.integration
def test_ai_cache_singleflight_uses_postgres_and_redis() -> None:
    database_url = os.getenv("ASHARE_TEST_DATABASE_URL")
    redis_url = os.getenv("ASHARE_TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("set ASHARE_TEST_DATABASE_URL and ASHARE_TEST_REDIS_URL to run")

    engine = create_engine(database_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    redis_client = Redis.from_url(redis_url, decode_responses=True)
    user_id = str(uuid4())
    request_sha = "f" * 64
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            UserAccount(
                user_id=user_id,
                username=f"cache-{user_id}",
                password_hash="fixture",
                role="USER",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    calls = 0
    calls_lock = threading.Lock()

    def generate() -> AICacheGeneration:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.15)
        return AICacheGeneration(
            response={"recommendation": "hold"},
            model_name="integration-fixture",
            reasoning_effort="low",
        )

    service = AIResultCacheService(session_factory=factory, redis_client=redis_client)
    kwargs = {
        "user_id": user_id,
        "purpose": "EXIT_ADVICE",
        "request_sha256": request_sha,
        "prompt_version": "integration-v1",
        "ttl_seconds": 60,
        "validate": lambda value: value["recommendation"],
        "generate": generate,
    }
    try:
        assert redis_client.ping()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: service.get_or_generate(**kwargs), range(2)))
        with factory() as session:
            row = session.scalar(
                select(AIResponseCacheRow).where(
                    AIResponseCacheRow.user_id == user_id,
                    AIResponseCacheRow.request_sha256 == request_sha,
                )
            )
            assert row is not None
            assert row.hit_count == 1
        assert calls == 1
        assert sorted(result.cache_hit for result in results) == [False, True]
        assert results[0].response == results[1].response == {"recommendation": "hold"}
    finally:
        with factory() as session:
            session.query(AIResponseCacheRow).filter_by(user_id=user_id).delete()
            session.query(UserAccount).filter_by(user_id=user_id).delete()
            session.commit()
        redis_client.close()
        engine.dispose()
