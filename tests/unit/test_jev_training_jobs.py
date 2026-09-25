from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from ashare_ai.orchestration import jev_training_jobs


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    def exists(self, key: str) -> bool:
        return key in self.hashes

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, _key: str, _seconds: int) -> bool:
        return True

    def lpush(self, _key: str, _item: str) -> int:
        return 1


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        lake_root=tmp_path,
        jev_model_dir=tmp_path / "models",
        redis_url="redis://test",
        worker_lease_seconds=60,
    )


def test_auto_training_is_blocked_when_dataset_generator_has_no_files(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(jev_training_jobs, "get_settings", lambda: _settings(tmp_path))

    result = asyncio.run(jev_training_jobs.schedule_jev_auto_training(object(), force=True))

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "dataset_generator_unavailable"
    assert len(result["missing_paths"]) == 3


def test_worker_propagates_blocked_training_result(monkeypatch, tmp_path: Path) -> None:
    import redis

    client = FakeRedis()
    settings = _settings(tmp_path)
    monkeypatch.setattr(jev_training_jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: client)

    training_id = "training-1"
    payload_key, status_key = jev_training_jobs._keys(training_id)
    client.hset(payload_key, mapping={"payload": json.dumps({"force": True})})
    client.hset(status_key, mapping={"training_id": training_id, "status": "QUEUED"})

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("ashare_ai.storage.database.SessionLocal", lambda: Session())

    async def blocked(*_args, **_kwargs):
        return {"status": "BLOCKED", "reason": "trainer_integration_unavailable"}

    monkeypatch.setattr(jev_training_jobs, "schedule_jev_auto_training", blocked)

    jev_training_jobs.run_jev_training_job(training_id)

    status = client.hgetall(status_key)
    assert status["status"] == "BLOCKED"
    assert status["progress"] == "0"
    assert json.loads(status["result"])["reason"] == "trainer_integration_unavailable"
