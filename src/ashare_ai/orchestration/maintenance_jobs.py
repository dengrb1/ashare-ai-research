"""Periodic cleanup executed outside the long-lived serial worker."""

from __future__ import annotations

from typing import Any


def run_maintenance_job(_job_id: str) -> dict[str, Any]:
    from ashare_ai.agents.attachments import AttachmentService
    from ashare_ai.notifications.service import NotificationService
    from ashare_ai.orchestration.personal_archive_jobs import cleanup_expired_archives
    from ashare_ai.storage.database import SessionLocal

    with SessionLocal() as session:
        attachments = AttachmentService(session).cleanup_expired()
    archives = cleanup_expired_archives()
    with SessionLocal() as session:
        notifications = NotificationService(session).cleanup_expired()
        if notifications:
            session.commit()
    return {
        "expired_attachments": attachments,
        "expired_archives": archives,
        "expired_notifications": notifications,
    }
