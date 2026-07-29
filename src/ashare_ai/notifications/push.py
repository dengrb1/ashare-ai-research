from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.storage.models import (
    NotificationRow,
    PushDeliveryRow,
    PushDeviceRow,
)


class PushConfigurationError(RuntimeError):
    pass


class PushDeviceService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def register(
        self,
        *,
        user_id: str,
        installation_id: str,
        registration_id: str,
        app_version: str | None = None,
        os_version: str | None = None,
        device_model: str | None = None,
        now: datetime | None = None,
    ) -> PushDeviceRow:
        current = _utc(now)
        installation = installation_id.strip()[:64]
        registration = registration_id.strip()
        if not installation or not registration or len(registration) > 512:
            raise ValueError("invalid push registration")
        row = self.session.scalar(
            select(PushDeviceRow).where(
                PushDeviceRow.user_id == user_id,
                PushDeviceRow.installation_id == installation,
            )
        )
        encrypted = self._cipher().encrypt(registration.encode()).decode("ascii")
        fingerprint = hashlib.sha256(registration.encode()).hexdigest()
        if row is None:
            row = PushDeviceRow(
                user_id=user_id,
                installation_id=installation,
                provider="MIPUSH",
                encrypted_registration_id=encrypted,
                registration_fingerprint=fingerprint,
                created_at=current,
                updated_at=current,
            )
            self.session.add(row)
        else:
            row.encrypted_registration_id = encrypted
            row.registration_fingerprint = fingerprint
            row.updated_at = current
            row.disabled_at = None
        row.app_version = app_version.strip()[:32] if app_version else None
        row.os_version = os_version.strip()[:32] if os_version else None
        row.device_model = device_model.strip()[:96] if device_model else None
        self.session.flush()
        return row

    def disable(self, *, user_id: str, device_id: str, now: datetime | None = None) -> bool:
        row = self.session.scalar(
            select(PushDeviceRow).where(
                PushDeviceRow.user_id == user_id,
                PushDeviceRow.device_id == device_id,
            )
        )
        if row is None:
            return False
        row.disabled_at = _utc(now)
        row.updated_at = row.disabled_at
        self.session.flush()
        return True

    def registration_id(self, row: PushDeviceRow) -> str:
        try:
            return self._cipher().decrypt(row.encrypted_registration_id.encode()).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise PushConfigurationError("push registration cannot be decrypted") from exc

    def acknowledge(
        self,
        *,
        user_id: str,
        device_id: str,
        notification_id: str,
        status: str,
        now: datetime | None = None,
    ) -> PushDeliveryRow | None:
        row = self.session.scalar(
            select(PushDeliveryRow)
            .join(PushDeviceRow, PushDeviceRow.device_id == PushDeliveryRow.device_id)
            .where(
                PushDeviceRow.user_id == user_id,
                PushDeliveryRow.device_id == device_id,
                PushDeliveryRow.notification_id == notification_id,
            )
        )
        if row is None:
            return None
        current = _utc(now)
        row.status = "OPENED" if status == "OPENED" else "DELIVERED"
        row.delivered_at = row.delivered_at or current
        row.updated_at = current
        self.session.flush()
        return row

    def _cipher(self) -> MultiFernet:
        raw = (
            self.settings.personal_data_encryption_keys
            or self.settings.model_settings_encryption_keys
            or ""
        )
        keys = [part.strip().encode() for part in raw.split(",") if part.strip()]
        if not keys:
            raise PushConfigurationError("push registration encryption is not configured")
        try:
            return MultiFernet([Fernet(key) for key in keys])
        except (TypeError, ValueError) as exc:
            raise PushConfigurationError("push registration encryption is invalid") from exc


class MiPushSender:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.devices = PushDeviceService(session, self.settings)

    def send_due(self, *, limit: int = 100, now: datetime | None = None) -> int:
        current = _utc(now)
        rows = list(
            self.session.scalars(
                select(PushDeliveryRow)
                .where(
                    PushDeliveryRow.status.in_(("PENDING", "RETRY")),
                    or_(
                        PushDeliveryRow.next_attempt_at.is_(None),
                        PushDeliveryRow.next_attempt_at <= current,
                    ),
                )
                .order_by(PushDeliveryRow.created_at)
                .limit(max(1, min(limit, 500)))
            ).all()
        )
        for row in rows:
            self._send(row, current)
        return len(rows)

    def _send(self, delivery: PushDeliveryRow, now: datetime) -> None:
        device = self.session.get(PushDeviceRow, delivery.device_id)
        notice = self.session.get(NotificationRow, delivery.notification_id)
        if device is None or notice is None or device.disabled_at is not None:
            delivery.status = "CANCELLED"
            delivery.updated_at = now
            return
        if not self.settings.mipush_app_secret or not self.settings.mipush_package_name:
            self._retry(delivery, "NOT_CONFIGURED", now)
            return
        focus = {
            "island_name": notice.title[:30],
            "island_description": notice.body[:80],
            "island_priority": 2 if notice.severity in {"HIGH", "CRITICAL"} else 1,
        }
        form = {
            "registration_id": self.devices.registration_id(device),
            "restricted_package_name": self.settings.mipush_package_name,
            "title": notice.title,
            "description": notice.body[:128],
            "payload": json.dumps(
                {"notification_id": notice.notification_id}, separators=(",", ":")
            ),
            "notify_type": "-1",
            "extra.notify_foreground": "1",
            "extra.notify_effect": "1",
            "extra.channel_id": "alert" if notice.severity in {"HIGH", "CRITICAL"} else "progress",
            "extra.miui.focus.param": json.dumps(focus, ensure_ascii=False, separators=(",", ":")),
        }
        try:
            response = httpx.post(
                self.settings.mipush_api_url,
                data=form,
                headers={"Authorization": f"key={self.settings.mipush_app_secret}"},
                timeout=10,
            )
            payload = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            code = str(payload.get("code", response.status_code))
            if response.is_success and code in {"0", "200"}:
                delivery.status = "SENT"
                delivery.provider_message_id = (
                    str(payload.get("data", {}).get("id") or "")[:128] or None
                )
                delivery.error_code = None
                delivery.next_attempt_at = None
            elif code in {"70000003", "70000004", "70000006"}:
                device.disabled_at = now
                device.updated_at = now
                delivery.status = "INVALID"
                delivery.error_code = "INVALID_REGISTRATION"
            else:
                self._retry(delivery, f"PROVIDER_{code}"[:48], now)
        except (httpx.HTTPError, RuntimeError, ValueError):
            self._retry(delivery, "TRANSPORT_ERROR", now)
        delivery.attempt_count += 1
        delivery.updated_at = now

    @staticmethod
    def _retry(delivery: PushDeliveryRow, code: str, now: datetime) -> None:
        delivery.status = "FAILED" if delivery.attempt_count >= 5 else "RETRY"
        delivery.error_code = code
        if delivery.status == "RETRY":
            delivery.next_attempt_at = now + timedelta(minutes=min(60, 2**delivery.attempt_count))


def enqueue_notification_deliveries(
    session: Session, notification: NotificationRow, now: datetime
) -> None:
    devices = session.scalars(
        select(PushDeviceRow).where(
            PushDeviceRow.user_id == notification.user_id,
            PushDeviceRow.disabled_at.is_(None),
        )
    ).all()
    for device in devices:
        exists = session.scalar(
            select(PushDeliveryRow.delivery_id).where(
                PushDeliveryRow.notification_id == notification.notification_id,
                PushDeliveryRow.device_id == device.device_id,
            )
        )
        if exists is None:
            session.add(
                PushDeliveryRow(
                    notification_id=notification.notification_id,
                    device_id=device.device_id,
                    status="PENDING",
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
