from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import sha256_bytes
from ashare_ai.orchestration.redis_queue import RedisLeasedQueue
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import PersonalArchiveJob
from ashare_ai.storage.personal_archive import (
    PersonalArchiveError,
    apply_profile,
    build_personal_archive,
    delete_imported_archive_objects,
    delete_private_archive,
    load_profile,
    preview_profile,
    read_private_archive,
    unwrap_job_secret,
    write_private_archive,
)

PENDING_QUEUE = "ashare:personal-archive:pending"
PROCESSING_QUEUE = "ashare:personal-archive:processing"


def _queue() -> RedisLeasedQueue:
    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    return RedisLeasedQueue(
        client,
        pending=PENDING_QUEUE,
        processing=PROCESSING_QUEUE,
        lease_seconds=get_settings().worker_lease_seconds,
    )


def enqueue_personal_archive(archive_id: str) -> None:
    _queue().enqueue(archive_id)


def run_personal_archive_job(archive_id: str) -> dict[str, Any]:
    created_output_uri: str | None = None
    with SessionLocal() as session:
        job = _locked_job(session, archive_id)
        if job is None:
            raise KeyError(archive_id)
        if job.status != "PENDING":
            return job.result or {"status": job.status}
        _ensure_active(job)
        job.status = "PROCESSING"
        job.phase = "VALIDATING"
        job.progress = 5
        job.started_at = datetime.now(UTC)
        session.commit()
        kind = job.kind
        user_id = job.user_id
        source_archive_id = job.source_archive_id
        encrypted_secret = job.encrypted_secret
        source_uri = job.source_object_uri

    try:
        if not encrypted_secret:
            raise PersonalArchiveError(
                "档案一次性口令缺失", code="ARCHIVE_SECRET_UNAVAILABLE"
            )
        secret_owner = source_archive_id if kind == "IMPORT_APPLY" else archive_id
        passphrase = unwrap_job_secret(encrypted_secret, secret_owner or archive_id)
        if kind == "EXPORT":
            with SessionLocal() as session:
                _set_progress(session, archive_id, "COLLECTING", 25)
                payload = build_personal_archive(session, user_id, passphrase)
            result = {
                "byte_size": len(payload),
                "sha256": sha256_bytes(payload),
                "retention_hours": 24,
            }
            with SessionLocal() as session:
                job = _locked_job(session, archive_id)
                assert job is not None
                _ensure_active(job)
                uri = write_private_archive(
                    user_id, archive_id, "profile.ashare", payload
                )
                created_output_uri = uri
                job.output_object_uri = uri
                job.output_sha256 = str(result["sha256"])
                _succeed(job, result)
                job.encrypted_secret = None
                session.commit()
            return result
        if not source_uri:
            raise PersonalArchiveError(
                "导入档案缺失", code="ARCHIVE_SOURCE_MISSING"
            )
        payload = read_private_archive(source_uri)
        profile, objects = load_profile(payload, passphrase)
        if kind == "IMPORT_PREVIEW":
            with SessionLocal() as session:
                result = preview_profile(session, user_id, profile)
                job = _locked_job(session, archive_id)
                assert job is not None
                _ensure_active(job)
                _succeed(job, result)
                session.commit()
            return result
        if kind != "IMPORT_APPLY":
            raise PersonalArchiveError("档案任务类型无效", code="ARCHIVE_JOB_INVALID")
        with SessionLocal() as session:
            locked = list(
                session.scalars(
                    select(PersonalArchiveJob)
                    .where(
                        PersonalArchiveJob.archive_id.in_(
                            [
                                item
                                for item in (archive_id, source_archive_id)
                                if item is not None
                            ]
                        )
                    )
                    .order_by(PersonalArchiveJob.archive_id)
                    .with_for_update()
                ).all()
            )
            by_id = {item.archive_id: item for item in locked}
            job = by_id.get(archive_id)
            source_job = by_id.get(source_archive_id or "")
            if job is None:
                raise KeyError(archive_id)
            _ensure_active(job)
            if source_job is None:
                raise PersonalArchiveError(
                    "导入来源任务不存在", code="ARCHIVE_SOURCE_MISSING"
                )
            _ensure_active(source_job)
            job.phase = "APPLYING"
            job.progress = 55
            result = apply_profile(
                session,
                user_id=user_id,
                profile=profile,
                objects=objects,
                merge_options=job.merge_options,
                archive_id=archive_id,
            )
            _succeed(job, result)
            job.encrypted_secret = None
            session.commit()
        return result
    except Exception as exc:
        code = exc.code if isinstance(exc, PersonalArchiveError) else "ARCHIVE_INTERNAL_ERROR"
        if kind == "EXPORT":
            delete_private_archive(created_output_uri)
        if kind == "IMPORT_APPLY":
            delete_imported_archive_objects(user_id, archive_id)
        with SessionLocal() as session:
            job = _locked_job(session, archive_id)
            if job is not None and job.deleted_at is None and job.status != "CANCELLED":
                job.status = "FAILED"
                job.phase = "FAILED"
                job.error_code = code
                job.result = {"message": _safe_failure_message(code)}
                job.completed_at = datetime.now(UTC)
                job.encrypted_secret = None
                session.commit()
        raise


def cleanup_expired_archives(now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    with SessionLocal() as session:
        rows = session.scalars(
            select(PersonalArchiveJob).where(
                PersonalArchiveJob.expires_at <= current,
                PersonalArchiveJob.deleted_at.is_(None),
            )
            .order_by(PersonalArchiveJob.archive_id)
            .with_for_update()
        ).all()
        for row in rows:
            delete_private_archive(row.source_object_uri)
            delete_private_archive(row.output_object_uri)
            row.deleted_at = current
            row.encrypted_secret = None
            if row.status in {"PENDING", "PROCESSING"}:
                row.status = "CANCELLED"
                row.phase = "EXPIRED"
        session.commit()
        return len(rows)


def _set_progress(session: Session, archive_id: str, phase: str, progress: int) -> None:
    job = _locked_job(session, archive_id)
    if job is not None:
        _ensure_active(job)
        job.phase = phase
        job.progress = progress
        session.commit()


def _succeed(job: PersonalArchiveJob, result: dict[str, Any]) -> None:
    job.status = "SUCCEEDED"
    job.phase = "COMPLETED"
    job.progress = 100
    job.result = result
    job.error_code = None
    job.completed_at = datetime.now(UTC)


def _locked_job(session: Session, archive_id: str) -> PersonalArchiveJob | None:
    return session.scalar(
        select(PersonalArchiveJob)
        .where(PersonalArchiveJob.archive_id == archive_id)
        .with_for_update()
    )


def _ensure_active(job: PersonalArchiveJob) -> None:
    if (
        job.deleted_at is not None
        or job.status == "CANCELLED"
        or job.expires_at <= datetime.now(UTC)
    ):
        raise PersonalArchiveError("档案任务已取消或过期", code="ARCHIVE_JOB_CANCELLED")


def _safe_failure_message(code: str) -> str:
    if code == "ARCHIVE_AUTHENTICATION_FAILED":
        return "口令错误或档案已损坏"
    if code == "ARCHIVE_VERSION_UNSUPPORTED":
        return "档案版本不兼容"
    if code == "ARCHIVE_HASH_MISMATCH":
        return "档案完整性校验失败"
    return "个人档案处理失败"
