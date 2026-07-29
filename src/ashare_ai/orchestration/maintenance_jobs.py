"""Periodic cleanup executed outside the long-lived serial worker."""

from __future__ import annotations

from typing import Any


def run_maintenance_job(_job_id: str) -> dict[str, Any]:
    from ashare_ai.agents.attachments import AttachmentService
    from ashare_ai.core.config import get_settings
    from ashare_ai.notifications.push import MiPushSender
    from ashare_ai.notifications.service import NotificationService
    from ashare_ai.orchestration.personal_archive_jobs import cleanup_expired_archives
    from ashare_ai.storage.database import SessionLocal

    with SessionLocal() as session:
        attachments = AttachmentService(session).cleanup_expired()
    archives = cleanup_expired_archives()
    with SessionLocal() as session:
        settings = get_settings()
        push_deliveries = 0
        if settings.mipush_app_secret and settings.mipush_package_name:
            push_deliveries = MiPushSender(session, settings).send_due()
        notifications = NotificationService(session).cleanup_expired()
        if notifications or push_deliveries:
            session.commit()
    return {
        "expired_attachments": attachments,
        "expired_archives": archives,
        "expired_notifications": notifications,
        "push_deliveries": push_deliveries,
    }
