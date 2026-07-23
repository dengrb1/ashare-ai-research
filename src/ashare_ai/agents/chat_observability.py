from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import AIChatMetricRow

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
        session.scalars(
            select(AIChatMetricRow).where(AIChatMetricRow.user_id == user_id)
        ).all()
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
