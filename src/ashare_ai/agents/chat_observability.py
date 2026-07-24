from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ashare_ai.agents.model_settings import ModelConfigurationService, ModelRuntimeProfile
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    AgentCall,
    AIChatMessage,
    AIChatMetricRow,
    AIChatThread,
    AIResponseCacheRow,
    JobRun,
)

_METRICS = ("answer", "context", "market", "news", "model")


def record_chat_metric(
    *,
    user_id: str,
    metric: str,
    hit: bool,
    latency_ms: int = 0,
    singleflight_wait_ms: int = 0,
    degraded: bool = False,
    session: Session | None = None,
    now: datetime | None = None,
) -> None:
    """Aggregate user-isolated cache and latency counters without prompt content."""

    if metric not in _METRICS:
        return
    current = (now or datetime.now(UTC)).astimezone(UTC)
    owns_session = session is None
    db = session or SessionLocal()
    try:
        row = db.scalar(
            select(AIChatMetricRow).where(
                AIChatMetricRow.user_id == user_id,
                AIChatMetricRow.metric == metric,
                AIChatMetricRow.bucket_date == current.date(),
            )
        )
        if row is None:
            row = AIChatMetricRow(
                user_id=user_id,
                metric=metric,
                bucket_date=current.date(),
                request_count=0,
                hit_count=0,
                latency_ms_total=0,
                singleflight_wait_ms_total=0,
                degraded_count=0,
                updated_at=current,
            )
            db.add(row)
        row.request_count += 1
        row.hit_count += int(hit)
        row.latency_ms_total += max(0, int(latency_ms))
        row.singleflight_wait_ms_total += max(0, int(singleflight_wait_ms))
        row.degraded_count += int(degraded)
        row.updated_at = current
        if owns_session:
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        # First-use metric writes can race across two chat requests. Metrics are
        # observational, so an independent session retries the now-created row
        # once instead of making the answer path fail.
        if not owns_session:
            raise
        db.rollback()
        row = db.scalar(
            select(AIChatMetricRow).where(
                AIChatMetricRow.user_id == user_id,
                AIChatMetricRow.metric == metric,
                AIChatMetricRow.bucket_date == current.date(),
            )
        )
        if row is not None:
            row.request_count += 1
            row.hit_count += int(hit)
            row.latency_ms_total += max(0, int(latency_ms))
            row.singleflight_wait_ms_total += max(0, int(singleflight_wait_ms))
            row.degraded_count += int(degraded)
            row.updated_at = current
            db.commit()
    finally:
        if owns_session:
            db.close()


def chat_metric_summary(session: Session, user_id: str) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(select(AIChatMetricRow).where(AIChatMetricRow.user_id == user_id)).all()
    )
    grouped: dict[str, dict[str, int]] = {
        metric: {"requests": 0, "hits": 0, "latency": 0, "wait": 0, "degraded": 0}
        for metric in _METRICS
    }
    for row in rows:
        if row.metric not in grouped:
            continue
        total = grouped[row.metric]
        total["requests"] += row.request_count
        total["hits"] += row.hit_count
        total["latency"] += row.latency_ms_total
        total["wait"] += row.singleflight_wait_ms_total
        total["degraded"] += row.degraded_count
    return [
        {
            "metric": metric,
            "requests": item["requests"],
            "hits": item["hits"],
            "hit_rate": item["hits"] / item["requests"] if item["requests"] else 0.0,
            "average_latency_ms": item["latency"] / item["requests"] if item["requests"] else 0.0,
            "average_singleflight_wait_ms": item["wait"] / item["requests"]
            if item["requests"]
            else 0.0,
            "degraded_count": item["degraded"],
        }
        for metric, item in grouped.items()
    ]


def chat_cost_summary(
    session: Session,
    user_id: str,
    *,
    days: int = 30,
    before: date | None = None,
    limit: int = 30,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Return user-scoped cost estimates without returning prompts or upstream data."""

    current = datetime.now(UTC)
    end_date = before - timedelta(days=1) if before else current.date()
    start_date = end_date - timedelta(days=max(1, days) - 1)
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    try:
        runtime = ModelConfigurationService().resolve(session, require_enabled=False)
    except Exception:
        # Cost reporting remains safe and user-isolated even when an old encrypted
        # configuration can no longer be resolved; unknown prices estimate to zero.
        runtime = None

    buckets: dict[date, dict[str, Any]] = {}
    latest_turn: tuple[datetime, dict[str, Any]] | None = None

    def add(
        *,
        at: datetime,
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        cache_hit: bool,
        is_turn: bool = False,
    ) -> None:
        profile = (
            runtime.profile_for(model) if runtime is not None else ModelRuntimeProfile(model=model)
        )
        estimate = _cost_estimate(
            profile,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            local_cache_hit=cache_hit,
        )
        bucket = buckets.setdefault(at.astimezone(UTC).date(), _empty_cost_bucket())
        _add_cost_value(bucket, estimate, cache_hit=cache_hit)
        nonlocal latest_turn
        if is_turn and (latest_turn is None or at > latest_turn[0]):
            latest_turn = (at, {**estimate, "cache_hit": cache_hit})

    chat_query = (
        select(AIChatMessage)
        .join(AIChatThread, AIChatThread.thread_id == AIChatMessage.thread_id)
        .where(
            AIChatThread.user_id == user_id,
            AIChatMessage.role == "assistant",
            AIChatMessage.status == "COMPLETED",
            AIChatMessage.created_at >= start,
            AIChatMessage.created_at < end,
        )
    )
    for row in session.scalars(chat_query).all():
        add(
            at=row.created_at,
            model=row.model_name or "unknown",
            input_tokens=row.input_tokens,
            cached_input_tokens=row.cached_input_tokens,
            cache_write_tokens=row.cache_write_tokens,
            output_tokens=row.output_tokens,
            cache_hit=row.cache_hit,
            is_turn=row.thread_id == thread_id,
        )

    for cache_row in session.scalars(
        select(AIResponseCacheRow).where(
            AIResponseCacheRow.user_id == user_id,
            AIResponseCacheRow.purpose != "CHAT",
            AIResponseCacheRow.created_at >= start,
            AIResponseCacheRow.created_at < end,
        )
    ).all():
        add(
            at=cache_row.created_at,
            model=cache_row.model_name or "unknown",
            input_tokens=cache_row.input_tokens,
            cached_input_tokens=cache_row.cached_input_tokens,
            cache_write_tokens=cache_row.cache_write_tokens,
            output_tokens=cache_row.output_tokens,
            cache_hit=False,
        )
    for agent_call in session.scalars(
        select(AgentCall)
        .join(JobRun, JobRun.run_id == AgentCall.run_id)
        .where(
            JobRun.user_id == user_id,
            AgentCall.created_at >= start,
            AgentCall.created_at < end,
        )
    ).all():
        add(
            at=agent_call.created_at,
            model=agent_call.model_name or "unknown",
            input_tokens=agent_call.input_tokens,
            cached_input_tokens=agent_call.cached_input_tokens,
            cache_write_tokens=agent_call.cache_write_tokens,
            output_tokens=agent_call.output_tokens,
            cache_hit=False,
        )

    ordered_dates = sorted(buckets, reverse=True)
    page_dates = ordered_dates[:limit]
    totals = _empty_cost_bucket()
    for bucket in buckets.values():
        _add_cost_value(totals, bucket, cache_hit_count=bucket["cache_hits"])
    return {
        "days": days,
        "items": [{"bucket_date": item, **buckets[item]} for item in page_dates],
        "next_cursor": ordered_dates[limit].isoformat() if len(ordered_dates) > limit else None,
        "totals": totals,
        "current_turn": latest_turn[1] if latest_turn else None,
    }


def _empty_cost_bucket() -> dict[str, Any]:
    return {
        "requests": 0,
        "cache_hits": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "estimated_spend_usd": Decimal("0"),
        "estimated_savings_usd": Decimal("0"),
    }


def _cost_estimate(
    profile: ModelRuntimeProfile,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    local_cache_hit: bool,
) -> dict[str, Any]:
    input_count = max(0, int(input_tokens))
    cached_count = min(input_count, max(0, int(cached_input_tokens)))
    uncached = input_count - cached_count
    write_count = max(0, int(cache_write_tokens))
    output_count = max(0, int(output_tokens))
    million = Decimal("1000000")
    normal_input = Decimal(uncached) * profile.input_price_per_million / million
    cached_input = Decimal(cached_count) * profile.cached_input_price_per_million / million
    cache_write = Decimal(write_count) * profile.cache_write_price_per_million / million
    output = Decimal(output_count) * profile.output_price_per_million / million
    spend = Decimal("0") if local_cache_hit else normal_input + cached_input + cache_write + output
    saved = (
        Decimal(input_count) * profile.input_price_per_million / million + output
        if local_cache_hit
        else Decimal(cached_count)
        * max(
            Decimal("0"), profile.input_price_per_million - profile.cached_input_price_per_million
        )
        / million
    )
    return {
        "requests": 1,
        "input_tokens": input_count,
        "cached_input_tokens": cached_count,
        "cache_write_tokens": write_count,
        "uncached_input_tokens": uncached,
        "output_tokens": output_count,
        "estimated_spend_usd": spend,
        "estimated_savings_usd": saved,
    }


def _add_cost_value(
    target: dict[str, Any],
    value: dict[str, Any],
    *,
    cache_hit: bool = False,
    cache_hit_count: int | None = None,
) -> None:
    for key in (
        "requests",
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "estimated_spend_usd",
        "estimated_savings_usd",
    ):
        target[key] += value[key]
    target["cache_hits"] += cache_hit_count if cache_hit_count is not None else int(cache_hit)
