from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.notifications.push import enqueue_notification_deliveries
from ashare_ai.storage.models import NotificationRow, UserAccount


class InvalidNotificationCursor(ValueError):
    pass


class NotificationService:
    """Create and query durable notifications without exposing other users' data."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def create(
        self,
        *,
        user_id: str,
        notification_type: str,
        severity: str,
        title: str,
        body: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        now: datetime | None = None,
    ) -> NotificationRow:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if dedupe_key:
            existing = self.session.scalar(
                select(NotificationRow)
                .where(
                    NotificationRow.user_id == user_id,
                    NotificationRow.dedupe_key == dedupe_key,
                )
                .order_by(NotificationRow.created_at.desc())
                .limit(1)
            )
            if existing is not None:
                if _as_utc(existing.expires_at) > current:
                    return existing
                # The uniqueness constraint deliberately survives background cleanup.
                # Reuse an expired row so a recurring operational event can alert again
                # without depending on cleanup timing.
                existing.notification_type = notification_type[:48]
                existing.severity = (
                    severity if severity in {"INFO", "WARNING", "HIGH", "CRITICAL"} else "INFO"
                )
                existing.title = title.strip()[:160] or "系统通知"
                existing.body = body.strip()[:4000] or "暂无详情"
                existing.resource_type = resource_type[:32] if resource_type else None
                existing.resource_id = resource_id[:64] if resource_id else None
                existing.payload = _safe_payload(payload)
                existing.read_at = None
                existing.created_at = current
                existing.expires_at = current + timedelta(days=self._unread_retention_days())
                self.session.flush()
                return existing
        row = NotificationRow(
            user_id=user_id,
            notification_type=notification_type[:48],
            severity=severity if severity in {"INFO", "WARNING", "HIGH", "CRITICAL"} else "INFO",
            title=title.strip()[:160] or "系统通知",
            body=body.strip()[:4000] or "暂无详情",
            resource_type=resource_type[:32] if resource_type else None,
            resource_id=resource_id[:64] if resource_id else None,
            payload=_safe_payload(payload),
            dedupe_key=dedupe_key[:64] if dedupe_key else None,
            created_at=current,
            expires_at=current + timedelta(days=self._unread_retention_days()),
        )
        self.session.add(row)
        self.session.flush()
        enqueue_notification_deliveries(self.session, row, current)
        return row

    def create_for_administrators(
        self,
        *,
        notification_type: str,
        severity: str,
        title: str,
        body: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        now: datetime | None = None,
    ) -> list[NotificationRow]:
        administrators = list(
            self.session.scalars(
                select(UserAccount).where(
                    UserAccount.enabled.is_(True), UserAccount.role == "ADMIN"
                )
            ).all()
        )
        return [
            self.create(
                user_id=admin.user_id,
                notification_type=notification_type,
                severity=severity,
                title=title,
                body=body,
                resource_type=resource_type,
                resource_id=resource_id,
                payload=payload,
                dedupe_key=f"{dedupe_key}:{admin.user_id}" if dedupe_key else None,
                now=now,
            )
            for admin in administrators
        ]

    def list(
        self,
        user_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        unread_only: bool = False,
        now: datetime | None = None,
    ) -> tuple[list[NotificationRow], str | None]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        page_limit = min(max(1, limit), self._page_limit())
        statement = select(NotificationRow).where(
            NotificationRow.user_id == user_id, NotificationRow.expires_at > current
        )
        if unread_only:
            statement = statement.where(NotificationRow.read_at.is_(None))
        if cursor:
            cursor_at, cursor_id = _decode_cursor(cursor)
            statement = statement.where(
                or_(
                    NotificationRow.created_at < cursor_at,
                    and_(
                        NotificationRow.created_at == cursor_at,
                        NotificationRow.notification_id < cursor_id,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    NotificationRow.created_at.desc(), NotificationRow.notification_id.desc()
                ).limit(page_limit + 1)
            ).all()
        )
        has_more = len(rows) > page_limit
        items = rows[:page_limit]
        next_cursor = _encode_cursor(items[-1]) if has_more and items else None
        return items, next_cursor

    def summary(self, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        base = [
            NotificationRow.user_id == user_id,
            NotificationRow.expires_at > current,
            NotificationRow.read_at.is_(None),
        ]
        unread = int(self.session.scalar(select(func.count()).where(*base)) or 0)
        high_risk = int(
            self.session.scalar(
                select(func.count()).where(
                    *base, NotificationRow.severity.in_(("HIGH", "CRITICAL"))
                )
            )
            or 0
        )
        latest, _ = self.list(user_id, limit=5, unread_only=True, now=current)
        return {
            "unread_count": unread,
            "high_risk_unread_count": high_risk,
            "latest": latest,
        }

    def mark_read(
        self, user_id: str, notification_ids: Sequence[str], *, now: datetime | None = None
    ) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        rows = list(
            self.session.scalars(
                select(NotificationRow).where(
                    NotificationRow.user_id == user_id,
                    NotificationRow.notification_id.in_(list(dict.fromkeys(notification_ids))),
                )
            ).all()
        )
        changed = 0
        read_expiry = current + timedelta(days=self._read_retention_days())
        for row in rows:
            if row.read_at is None:
                row.read_at = current
                row.expires_at = read_expiry
                changed += 1
        if changed:
            self.session.flush()
        return changed

    def mark_all_read(self, user_id: str, *, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        rows = list(
            self.session.scalars(
                select(NotificationRow).where(
                    NotificationRow.user_id == user_id,
                    NotificationRow.read_at.is_(None),
                    NotificationRow.expires_at > current,
                )
            ).all()
        )
        return self.mark_read(user_id, [row.notification_id for row in rows], now=current)

    def delete(self, user_id: str, notification_ids: Sequence[str]) -> int:
        """Permanently remove a caller-owned set of notifications."""
        rows = list(
            self.session.scalars(
                select(NotificationRow).where(
                    NotificationRow.user_id == user_id,
                    NotificationRow.notification_id.in_(list(dict.fromkeys(notification_ids))),
                )
            ).all()
        )
        for row in rows:
            self.session.delete(row)
        if rows:
            self.session.flush()
        return len(rows)

    def clear_all(self, user_id: str) -> int:
        """Permanently remove every notification owned by one user."""
        rows = list(
            self.session.scalars(
                select(NotificationRow).where(NotificationRow.user_id == user_id)
            ).all()
        )
        for row in rows:
            self.session.delete(row)
        if rows:
            self.session.flush()
        return len(rows)

    def cleanup_expired(self, *, now: datetime | None = None, batch_size: int = 500) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        rows = list(
            self.session.scalars(
                select(NotificationRow)
                .where(NotificationRow.expires_at <= current)
                .order_by(NotificationRow.expires_at)
                .limit(max(1, min(batch_size, 2000)))
            ).all()
        )
        for row in rows:
            self.session.delete(row)
        if rows:
            self.session.flush()
        return len(rows)

    def _policy(self) -> dict[str, Any]:
        try:
            raw = json.loads(Path(self.settings.policy_config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return raw.get("notifications", {}) if isinstance(raw, dict) else {}

    def _read_retention_days(self) -> int:
        return _bounded_int(self._policy().get("read_retention_days"), default=90, low=1, high=3650)

    def _unread_retention_days(self) -> int:
        return _bounded_int(
            self._policy().get("unread_retention_days"), default=180, low=1, high=3650
        )

    def _page_limit(self) -> int:
        return _bounded_int(self._policy().get("page_limit"), default=50, low=1, high=200)


def _safe_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    # JSON encoding is also an inexpensive boundary against ORM objects and exception values.
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)[:8000]
        decoded = json.loads(encoded)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, parsed))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _encode_cursor(row: NotificationRow) -> str:
    payload = json.dumps(
        {"created_at": _as_utc(row.created_at).isoformat(), "id": row.notification_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        notification_id = str(payload["id"])
    except (KeyError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidNotificationCursor("invalid notification cursor") from exc
    if created_at.tzinfo is None or not notification_id:
        raise InvalidNotificationCursor("invalid notification cursor")
    return created_at.astimezone(UTC), notification_id
