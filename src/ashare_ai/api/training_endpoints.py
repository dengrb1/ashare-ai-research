"""Authenticated Jev model training controls backed by the job-worker queue."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

import redis
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ashare_ai.api.auth import AuthContext, require_admin
from ashare_ai.api.dependencies import get_auth_context, get_write_context
from ashare_ai.core.config import get_settings
from ashare_ai.orchestration.jev_training_jobs import (
    STATUS_PREFIX,
    cancel_jev_training,
    enqueue_jev_training,
    get_jev_training_status,
)

router = APIRouter(prefix="/api/v1/training", tags=["training"])
Current = Annotated[AuthContext, Depends(get_auth_context)]
Writer = Annotated[AuthContext, Depends(get_write_context)]


class TrainingTriggerRequest(BaseModel):
    force: bool = False
    dataset_config: dict[str, Any] | None = None


class TrainingStatusResponse(BaseModel):
    training_id: str
    status: str
    progress: float = Field(ge=0, le=1)
    current_epoch: int = 0
    total_epochs: int = 0
    metrics: dict[str, Any] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    estimated_completion: datetime | None = None
    error: str | None = None


class TrainingHistoryResponse(BaseModel):
    trainings: list[TrainingStatusResponse]
    total: int


class TrainingTriggerResponse(BaseModel):
    status: str = "queued"
    training_id: str
    estimated_duration_minutes: int = 120
    message: str


def _client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _response(status: dict[str, str]) -> TrainingStatusResponse:
    result: dict[str, Any] | None = None
    if status.get("result"):
        try:
            parsed = json.loads(status["result"])
            result = parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            result = {"raw": status["result"]}
    try:
        progress = float(status.get("progress", "0"))
    except ValueError:
        progress = 0.0
    if progress > 1:
        progress /= 100
    return TrainingStatusResponse(
        training_id=status.get("training_id", ""),
        status=status.get("status", "UNKNOWN").lower(),
        progress=max(0.0, min(1.0, progress)),
        current_epoch=int(status.get("current_epoch", "0") or 0),
        total_epochs=int(status.get("total_epochs", "0") or 0),
        metrics=result,
        started_at=_parse_datetime(status.get("started_at")),
        completed_at=_parse_datetime(status.get("completed_at")),
        estimated_completion=_parse_datetime(status.get("estimated_completion")),
        error=status.get("error"),
    )


def _require_admin(context: Current) -> AuthContext:
    require_admin(context)
    return context


Admin = Annotated[AuthContext, Depends(_require_admin)]


@router.post("/jev/trigger", response_model=TrainingTriggerResponse, status_code=202)
def trigger_jev_training(
    request: Annotated[TrainingTriggerRequest, Body(...)],
    context: Writer,
) -> TrainingTriggerResponse:
    require_admin(context)
    training_id = enqueue_jev_training(
        user_id=context.user.user_id,
        force=request.force,
        dataset_config=request.dataset_config,
    )
    return TrainingTriggerResponse(
        training_id=training_id,
        message="Jev 训练任务已进入 job-worker 队列",
    )


@router.get("/jev/status/{training_id}", response_model=TrainingStatusResponse)
def get_training_status(training_id: str, context: Admin) -> TrainingStatusResponse:
    status = get_jev_training_status(_client(), training_id)
    if status is None:
        raise HTTPException(status_code=404, detail="training job not found")
    return _response(status)


@router.get("/jev/status/current", response_model=TrainingStatusResponse)
def get_current_training_status(context: Admin) -> TrainingStatusResponse:
    client = _client()
    latest: dict[str, str] | None = None
    for key in client.scan_iter(match=f"{STATUS_PREFIX}*"):
        values = client.hgetall(key)
        if not values or latest is None or values.get("created_at", "") > latest.get("created_at", ""):
            latest = dict(values)
    if latest is None:
        return TrainingStatusResponse(training_id="none", status="idle", progress=0)
    return _response(latest)


@router.post("/jev/cancel/{training_id}", response_model=TrainingStatusResponse)
def cancel_training(training_id: str, context: Writer) -> TrainingStatusResponse:
    require_admin(context)
    status = cancel_jev_training(_client(), training_id)
    if status is None:
        raise HTTPException(status_code=404, detail="training job not found")
    return _response(status)


@router.get("/jev/history", response_model=TrainingHistoryResponse)
def get_training_history(
    context: Admin,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> TrainingHistoryResponse:
    client = _client()
    statuses: list[dict[str, str]] = []
    for key in client.scan_iter(match=f"{STATUS_PREFIX}*"):
        values = client.hgetall(key)
        if values:
            statuses.append(dict(values))
    statuses.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    selected = statuses[offset : offset + limit]
    return TrainingHistoryResponse(
        trainings=[_response(item) for item in selected],
        total=len(statuses),
    )
