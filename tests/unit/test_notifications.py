from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.notifications.service import NotificationService
from ashare_ai.storage.models import Base, UserAccount


def _engine():
    return create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _add_user(session: Session, user_id: str, now: datetime) -> None:
    session.add(
        UserAccount(
            user_id=user_id,
            username=user_id,
            password_hash="hash",
            role="USER",
            enabled=True,
            session_version=1,
            created_at=now,
            updated_at=now,
        )
    )


def test_notifications_are_user_isolated_and_read_retention_starts_when_read() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 20, 8, tzinfo=UTC)
    with Session(engine) as session:
        _add_user(session, "notice-user", now)
        _add_user(session, "other-user", now)
        service = NotificationService(session)
        own = service.create(
            user_id="notice-user",
            notification_type="STOP_LOSS_TRIGGERED",
            severity="CRITICAL",
            title="止损预警",
            body="仅模拟退出研究。",
            dedupe_key="notice-own",
            now=now,
        )
        service.create(
            user_id="other-user",
            notification_type="BUY_ENTRY_RANGE_HIT",
            severity="HIGH",
            title="其他用户通知",
            body="不应泄露。",
            dedupe_key="notice-other",
            now=now,
        )
        session.commit()

        items, cursor = service.list("notice-user", now=now)
        assert cursor is None
        assert [item.notification_id for item in items] == [own.notification_id]
        summary = service.summary("notice-user", now=now)
        assert summary["unread_count"] == 1
        assert summary["high_risk_unread_count"] == 1

        assert service.mark_read("notice-user", [own.notification_id], now=now) == 1
        assert own.read_at == now
        assert own.expires_at == now + timedelta(days=90)
        assert service.mark_read("other-user", [own.notification_id], now=now) == 0


def test_expired_dedupe_key_is_reused_without_waiting_for_cleanup() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 20, 8, tzinfo=UTC)
    with Session(engine) as session:
        _add_user(session, "dedupe-user", now)
        service = NotificationService(session)
        first = service.create(
            user_id="dedupe-user",
            notification_type="BUY_MONITOR_DATA_UNAVAILABLE",
            severity="WARNING",
            title="旧通知",
            body="旧正文",
            dedupe_key="data-window",
            now=now - timedelta(days=181),
        )
        session.commit()

        reused = service.create(
            user_id="dedupe-user",
            notification_type="BUY_ENTRY_RANGE_HIT",
            severity="HIGH",
            title="新通知",
            body="新正文",
            dedupe_key="data-window",
            now=now,
        )
        session.commit()

        assert reused.notification_id == first.notification_id
        assert reused.notification_type == "BUY_ENTRY_RANGE_HIT"
        assert reused.read_at is None
        assert reused.expires_at.replace(tzinfo=UTC) == now + timedelta(days=180)
