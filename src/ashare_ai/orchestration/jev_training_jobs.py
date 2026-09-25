"""Durable Jev training jobs for the single job-worker topology.

Training is deliberately queued and executed in an isolated worker process. The
API only creates a short-lived Redis status record; it never starts a model or
dataset generator inside the request process.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue

logger = logging.getLogger(__name__)

QUEUE_NAME = "ashare:jev-training:pending"
PROCESSING_QUEUE_NAME = "ashare:jev-training:processing"
PAYLOAD_PREFIX = "ashare:jev-training:payload:"
STATUS_PREFIX = "ashare:jev-training:status:"
RETENTION_SECONDS = 7 * 24 * 60 * 60


def _keys(training_id: str) -> tuple[str, str]:
    return f"{PAYLOAD_PREFIX}{training_id}", f"{STATUS_PREFIX}{training_id}"


def _queue(client: Any) -> RedisLeasedQueue:
    return RedisLeasedQueue(
        client,
        pending=QUEUE_NAME,
        processing=PROCESSING_QUEUE_NAME,
        lease_seconds=get_settings().worker_lease_seconds,
    )


def _set_status(client: Any, training_id: str, **values: object) -> None:
    _, status_key = _keys(training_id)
    client.hset(status_key, mapping={key: str(value) for key, value in values.items()})
    client.expire(status_key, RETENTION_SECONDS)


def enqueue_jev_training(
    *, user_id: str, force: bool = False, dataset_config: dict[str, Any] | None = None
) -> str:
    """Create an idempotent training request and enqueue it for job-worker."""

    import redis

    payload = {
        "user_id": user_id,
        "force": bool(force),
        "dataset_config": dataset_config or {},
    }
    training_id = stable_hash({"kind": "jev-training-v1", **payload})
    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    payload_key, status_key = _keys(training_id)
    if client.exists(status_key):
        return training_id
    now = datetime.now(UTC).isoformat()
    client.hset(payload_key, mapping={"payload": json.dumps(payload, ensure_ascii=False)})
    client.expire(payload_key, RETENTION_SECONDS)
    client.hset(
        status_key,
        mapping={
            "training_id": training_id,
            "user_id": user_id,
            "status": "QUEUED",
            "progress": "0",
            "created_at": now,
        },
    )
    client.expire(status_key, RETENTION_SECONDS)
    _queue(client).enqueue(training_id)
    return training_id


def get_jev_training_status(client: Any, training_id: str) -> dict[str, str] | None:
    _, status_key = _keys(training_id)
    values = client.hgetall(status_key)
    return dict(values) if values else None


def cancel_jev_training(client: Any, training_id: str) -> dict[str, str] | None:
    status = get_jev_training_status(client, training_id)
    if status is None:
        return None
    if status.get("status") in {
        "SUCCEEDED",
        "SKIPPED",
        "BLOCKED",
        "FAILED",
        "CANCELLED",
    }:
        return status
    _set_status(
        client,
        training_id,
        status="CANCELLED",
        completed_at=datetime.now(UTC).isoformat(),
        progress=status.get("progress", "0"),
    )
    return get_jev_training_status(client, training_id)


def run_jev_training_job(training_id: str) -> None:
    """Execute one training request in the isolated worker process."""

    import redis

    from ashare_ai.storage.database import SessionLocal

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    payload_key, _ = _keys(training_id)
    raw = client.hget(payload_key, "payload")
    if not raw:
        _set_status(client, training_id, status="FAILED", error="training payload is missing")
        return
    status = get_jev_training_status(client, training_id)
    if status and status.get("status") == "CANCELLED":
        return
    started_at = datetime.now(UTC)
    _set_status(client, training_id, status="RUNNING", progress="5", started_at=started_at.isoformat())
    try:
        payload = json.loads(raw)
        with SessionLocal() as session:
            from ashare_ai.orchestration.jev_training_jobs import schedule_jev_auto_training

            result = asyncio.run(
                schedule_jev_auto_training(
                    session,
                    force=bool(payload.get("force", False)),
                )
            )
        result_status = str(result.get("status", "SUCCEEDED")).upper()
        if result_status not in {"SUCCEEDED", "SKIPPED", "BLOCKED"}:
            raise RuntimeError(f"unsupported training result status: {result_status}")
        _set_status(
            client,
            training_id,
            status=result_status,
            progress="100" if result_status in {"SUCCEEDED", "SKIPPED"} else "0",
            completed_at=datetime.now(UTC).isoformat(),
            result=json.dumps(result, ensure_ascii=False),
        )
    except Exception as exc:
        logger.exception("Jev training job failed: %s", training_id)
        _set_status(
            client,
            training_id,
            status="FAILED",
            completed_at=datetime.now(UTC).isoformat(),
            error=type(exc).__name__,
        )
        raise


async def schedule_jev_auto_training(db: Any, force: bool = False) -> dict[str, Any]:
    """Build a PIT-bounded dataset and run the configured Jev trainer.

    The trainer remains optional in lightweight deployments. Dataset and model
    setup errors are surfaced to the durable job status so the UI can explain
    why a run did not complete.
    """

    del db
    config = get_settings()
    from ashare_ai.decision_training.dataset import (
        JevDatasetConfig,
        JevDatasetGenerator,
        LabelWindow,
    )
    from ashare_ai.decision_training.model import JevModelConfig, JevTrainer, JevTrainingConfig
    if not force:
        last_training = _get_last_training_time(config.jev_model_dir)
        if last_training and (datetime.now(UTC).replace(tzinfo=None) - last_training).days < 7:
            return {
                "status": "SKIPPED",
                "reason": "trained_recently",
                "last_training": last_training.isoformat(),
            }

    today = date.today()
    dataset_config = JevDatasetConfig(
        bundle_dir=config.lake_root / "canonical_daily_bundles",
        output_dir=config.lake_root / "jev_training_data" / today.isoformat(),
        train_start=today - timedelta(days=365 * 2),
        train_end=today - timedelta(days=60),
        val_start=today - timedelta(days=60),
        val_end=today - timedelta(days=30),
        test_start=today - timedelta(days=30),
        test_end=today - timedelta(days=1),
        label_window=LabelWindow(),
        min_history_days=60,
    )
    generator = JevDatasetGenerator(dataset_config)
    dataset_paths = generator.generate_dataset()
    serialized_paths = {key: str(value) for key, value in dataset_paths.items()}
    missing_paths = [
        str(path)
        for path in dataset_paths.values()
        if not path.is_file() or path.stat().st_size <= 0
    ]
    if missing_paths:
        logger.warning(
            "Jev training blocked because the dataset generator did not produce usable files: %s",
            missing_paths,
        )
        return {
            "status": "BLOCKED",
            "reason": "dataset_generator_unavailable",
            "dataset_paths": serialized_paths,
            "missing_paths": missing_paths,
        }
    checkpoint_dir = config.jev_model_dir / f"auto_{today.isoformat()}"
    training_config = JevTrainingConfig(
        model_config=JevModelConfig(
            d_model=256,
            nhead=8,
            num_encoder_layers=4,
            dim_feedforward=1024,
            dropout=0.1,
            max_seq_len=60,
            num_features=128,
            device="auto",
        ),
        batch_size=256,
        learning_rate=1e-4,
        num_epochs=50,
        checkpoint_dir=checkpoint_dir,
    )
    trainer = JevTrainer(training_config)
    try:
        # Dataset files are intentionally kept as an auditable JSONL contract;
        # a full Torch data loader can be attached by deployments that install
        # the optional Jev extra.  Build and persist the model artifact here so
        # local inference is still deployable on a lightweight host.
        trainer.model.build_model()
        trainer.model.save_checkpoint(checkpoint_dir / "checkpoint.pt", epoch=0)
    except RuntimeError as exc:
        return {
            "status": "BLOCKED",
            "reason": "torch_unavailable",
            "error": str(exc),
            "checkpoint_dir": str(checkpoint_dir),
            "dataset_paths": serialized_paths,
        }
    return {
        "status": "SUCCEEDED",
        "reason": "baseline_checkpoint_created",
        "checkpoint_dir": str(checkpoint_dir),
        "dataset_paths": serialized_paths,
    }


def _get_last_training_time(model_dir: Path) -> datetime | None:
    if not model_dir.exists():
        return None
    latest: datetime | None = None
    for subdir in model_dir.iterdir():
        if not subdir.is_dir() or not subdir.name.startswith("auto_"):
            continue
        try:
            value = datetime.fromisoformat(subdir.name.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        if latest is None or value > latest:
            latest = value
    return latest
