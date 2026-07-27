from __future__ import annotations

from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.core.config import Settings
from ashare_ai.notifications.push import MiPushSender, PushDeviceService
from ashare_ai.notifications.service import NotificationService
from ashare_ai.storage.models import Base, PushDeliveryRow, PushDeviceRow, UserAccount


def _session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _settings() -> Settings:
    return Settings(
        personal_data_encryption_keys=Fernet.generate_key().decode(),
        mipush_app_secret="server-only-secret",
        mipush_package_name="com.example.app",
    )


def test_registration_is_encrypted_user_owned_and_notification_is_enqueued() -> None:
    session = _session()
    now = datetime(2026, 7, 27, tzinfo=UTC)
    users = [
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
        for user_id in ("alice", "bob")
    ]
    session.add_all(users)
    session.flush()
    service = PushDeviceService(session, _settings())
    alice = service.register(
        user_id="alice",
        installation_id="alice-installation",
        registration_id="private-registration-id",
        now=now,
    )
    bob = service.register(
        user_id="bob",
        installation_id="bob-installation",
        registration_id="bob-registration-id",
        now=now,
    )
    assert "private-registration-id" not in alice.encrypted_registration_id
    assert service.registration_id(alice) == "private-registration-id"
    assert not service.disable(user_id="bob", device_id=alice.device_id, now=now)
    assert service.disable(user_id="bob", device_id=bob.device_id, now=now)

    notice = NotificationService(session).create(
        user_id="alice",
        notification_type="RISK",
        severity="HIGH",
        title="风险提醒",
        body="请查看详情",
        dedupe_key="risk-once",
        now=now,
    )
    NotificationService(session).create(
        user_id="alice",
        notification_type="RISK",
        severity="HIGH",
        title="风险提醒",
        body="请查看详情",
        dedupe_key="risk-once",
        now=now,
    )
    deliveries = session.scalars(
        select(PushDeliveryRow).where(PushDeliveryRow.notification_id == notice.notification_id)
    ).all()
    assert len(deliveries) == 1
    assert deliveries[0].device_id == alice.device_id
    session.close()


def test_sender_retries_without_persisting_provider_body(monkeypatch) -> None:
    session = _session()
    settings = _settings()
    now = datetime(2026, 7, 27, tzinfo=UTC)
    user = UserAccount(
        user_id="alice",
        username="alice",
        password_hash="hash",
        role="USER",
        enabled=True,
        session_version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.flush()
    PushDeviceService(session, settings).register(
        user_id="alice",
        installation_id="alice-installation",
        registration_id="private-registration-id",
        now=now,
    )
    NotificationService(session).create(
        user_id="alice",
        notification_type="RISK",
        severity="HIGH",
        title="风险提醒",
        body="请查看详情",
        now=now,
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider body containing server-only-secret")

    monkeypatch.setattr("ashare_ai.notifications.push.httpx.post", fail)
    assert MiPushSender(session, settings).send_due(now=now) == 1
    delivery = session.scalar(select(PushDeliveryRow))
    assert delivery is not None
    assert delivery.status == "RETRY"
    assert delivery.error_code == "TRANSPORT_ERROR"
    assert "secret" not in (delivery.error_code or "").lower()
    assert session.scalar(select(PushDeviceRow)) is not None
    session.close()
