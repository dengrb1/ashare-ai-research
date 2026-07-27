from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ashare_ai.agents.chat import _estimate_message_tokens, _select_history_within_budget
from ashare_ai.agents.chat_observability import _cost_estimate
from ashare_ai.agents.model_settings import ModelRuntimeProfile
from ashare_ai.agents.openai_compatible import OpenAICompatibleStructuredLLMClient, _usage
from ashare_ai.api.app import app
from ashare_ai.api.auth import hash_password
from ashare_ai.api.dependencies import get_db
from ashare_ai.storage.models import AIChatMessage, AIChatThread, Base, UserAccount


def test_usage_parser_normalizes_openai_and_grok_cache_fields() -> None:
    openai = _usage(
        {
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 60},
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 7},
            }
        }
    )
    grok = _usage(
        {
            "prompt_tokens": 90,
            "cache_write_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens": 11,
        }
    )

    assert (
        openai.input_tokens,
        openai.cached_input_tokens,
        openai.output_tokens,
        openai.reasoning_tokens,
    ) == (100, 60, 20, 7)
    assert (
        grok.input_tokens,
        grok.cached_input_tokens,
        grok.cache_write_tokens,
        grok.output_tokens,
    ) == (90, 40, 30, 11)


@pytest.mark.asyncio
@respx.mock
async def test_openai_cache_controls_fall_back_without_duplicate_answer() -> None:
    route = respx.post("http://llm.local/v1/responses").mock(
        side_effect=[
            httpx.Response(400, json={"error": "unknown cache field"}),
            httpx.Response(
                200,
                text=(
                    'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                    'data: {"type":"response.completed","response":{"id":"resp-1",'
                    '"usage":{"input_tokens":10,"input_tokens_details":{"cached_tokens":8},'
                    '"output_tokens":2}}}\n\n'
                ),
                headers={"content-type": "text/event-stream"},
            ),
        ]
    )
    client = OpenAICompatibleStructuredLLMClient(
        base_url="http://llm.local",
        api_key="secret",
        model="gpt-test",
        cache_policy="OPENAI",
        max_retries=0,
    )

    events = [
        event
        async for event in client.stream_text(
            messages=({"role": "user", "content": "question"},),
            idempotency_key="stable-request",
            previous_response_id="resp-previous",
            prompt_cache_key="prefix-key",
        )
    ]

    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert first["previous_response_id"] == "resp-previous"
    assert first["prompt_cache_key"] == "prefix-key"
    assert "previous_response_id" not in second
    assert "prompt_cache_key" not in second
    assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
        "stable-request",
        "stable-request",
    ]
    assert events[-1]["cached_input_tokens"] == 8
    assert events[-1]["response_id"] == "resp-1"


@pytest.mark.asyncio
@respx.mock
async def test_compatible_profile_omits_dedicated_cache_and_session_fields() -> None:
    route = respx.post("http://llm.local/v1/responses").mock(
        return_value=httpx.Response(
            200,
            text='data: {"type":"response.completed","response":{}}\n\n',
            headers={"content-type": "text/event-stream"},
        )
    )
    client = OpenAICompatibleStructuredLLMClient(
        base_url="http://llm.local",
        api_key="secret",
        model="proxy-model",
        cache_policy="COMPATIBLE",
    )
    _ = [
        event
        async for event in client.stream_text(
            messages=({"role": "user", "content": "question"},),
            idempotency_key="safe-request",
            previous_response_id="never-send",
            prompt_cache_key="never-send",
        )
    ]

    body = json.loads(route.calls.last.request.content)
    assert "previous_response_id" not in body
    assert "prompt_cache_key" not in body


def test_context_budget_drops_oldest_complete_turn_without_splitting_latest() -> None:
    history = [
        {"role": "system", "content": "old snapshot"},
        {"role": "user", "content": "old question" * 20},
        {"role": "assistant", "content": "old answer" * 20},
        {"role": "system", "content": "latest snapshot"},
        {"role": "user", "content": "latest question"},
        {"role": "assistant", "content": "latest answer"},
    ]
    dynamic = {"role": "system", "content": "fresh PIT context"}
    current = {"role": "user", "content": "current question"}
    fixed = sum(
        _estimate_message_tokens(item)
        for item in ({"role": "system", "content": "stable"}, dynamic, current)
    )
    latest_size = sum(_estimate_message_tokens(item) for item in history[3:])

    selected, status = _select_history_within_budget(
        stable_prompt="stable",
        history_messages=history,
        dynamic_message=dynamic,
        current_message=current,
        input_budget_tokens=fixed + latest_size,
    )

    assert status == "HISTORY_TRIMMED"
    assert selected == history[3:]
    rejected, rejected_status = _select_history_within_budget(
        stable_prompt="stable" * 100,
        history_messages=[],
        dynamic_message=dynamic,
        current_message=current,
        input_budget_tokens=1,
    )
    assert rejected is None
    assert rejected_status == "CONTEXT_TOO_LARGE"


def test_cost_estimate_keeps_local_answer_cache_out_of_spend() -> None:
    profile = ModelRuntimeProfile(
        model="priced-model",
        input_price_per_million=Decimal("2"),
        cached_input_price_per_million=Decimal("0.5"),
        cache_write_price_per_million=Decimal("3"),
        output_price_per_million=Decimal("8"),
    )
    remote = _cost_estimate(
        profile,
        input_tokens=1000,
        cached_input_tokens=400,
        cache_write_tokens=100,
        output_tokens=200,
        local_cache_hit=False,
    )
    local = _cost_estimate(
        profile,
        input_tokens=1000,
        cached_input_tokens=1000,
        cache_write_tokens=0,
        output_tokens=200,
        local_cache_hit=True,
    )

    assert remote["uncached_input_tokens"] == 600
    assert remote["estimated_spend_usd"] == Decimal("0.0033")
    assert local["estimated_spend_usd"] == Decimal("0")
    assert local["estimated_savings_usd"] == Decimal("0.0036")


def test_cost_summary_is_user_isolated_and_caps_days() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        owner = UserAccount(
            user_id="owner",
            username="cost-owner",
            password_hash=hash_password("owner-password"),
            role="USER",
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        other = UserAccount(
            user_id="other",
            username="cost-other",
            password_hash=hash_password("other-password"),
            role="USER",
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add_all((owner, other))
        session.add_all(
            (
                AIChatThread(
                    thread_id="owner-thread",
                    user_id="owner",
                    title="owner",
                    created_at=now,
                    updated_at=now,
                ),
                AIChatThread(
                    thread_id="other-thread",
                    user_id="other",
                    title="other",
                    created_at=now,
                    updated_at=now,
                ),
            )
        )
        for thread_id, content in (
            ("owner-thread", "owner reply"),
            ("other-thread", "other reply"),
        ):
            session.add(
                AIChatMessage(
                    thread_id=thread_id,
                    role="assistant",
                    content=content,
                    status="COMPLETED",
                    trading_date=now.date(),
                    decision_at=now,
                    available_at=now,
                    mentioned_symbols=[],
                    mention_refs=[],
                    attachment_ids=[],
                    sources=[],
                    cache_hit=False,
                    input_tokens=10,
                    output_tokens=2,
                    created_at=now,
                )
            )
        session.commit()

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "cost-owner", "password": "owner-password"}
            ).status_code
            == 200
        )
        response = client.get("/api/v1/ai/costs?days=30&thread_id=owner-thread")
        assert response.status_code == 200
        body = response.json()
        assert body["totals"]["requests"] == 1
        assert body["totals"]["input_tokens"] == 10
        assert body["current_turn"]["input_tokens"] == 10
        assert client.get("/api/v1/ai/costs?days=91").status_code == 422
        assert client.get("/api/v1/ai/costs?thread_id=other-thread").status_code == 404
    finally:
        app.dependency_overrides.clear()
