from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import AuthContext
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.notifications.service import NotificationService
from ashare_ai.storage.models import Base, ExitAdviceRow, UserAccount


def _database() -> tuple[Session, datetime]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False), datetime(2026, 7, 20, tzinfo=UTC)


def _context(user: UserAccount) -> AuthContext:
    return AuthContext(user=cast(Any, user), session=cast(Any, SimpleNamespace()))


def test_manual_exit_replays_idempotently_and_notifications_stay_user_scoped(monkeypatch) -> None:
    session, now = _database()
    alice = UserAccount(
        user_id="alice-risk",
        username="alice-risk",
        password_hash="hash",
        role="USER",
        enabled=True,
        session_version=1,
        created_at=now,
        updated_at=now,
    )
    bob = UserAccount(
        user_id="bob-risk",
        username="bob-risk",
        password_hash="hash",
        role="USER",
        enabled=True,
        session_version=1,
        created_at=now,
        updated_at=now,
    )
    session.add_all((alice, bob))
    session.commit()

    contexts = [_context(alice)]

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_auth_context] = lambda: contexts[0]
    app.dependency_overrides[get_write_context] = lambda: contexts[0]
    queued: list[str] = []
    created: list[str] = []

    def create_exit(db: Session, *, user_id: str, symbol: str) -> ExitAdviceRow:
        row = ExitAdviceRow(
            user_id=user_id,
            symbol=symbol,
            status="PENDING",
            decision_at=now,
            available_at=now,
            current_price=Decimal("10"),
            unrealized_profit=Decimal("0"),
            trigger_amount=Decimal("0"),
            trigger_type="MANUAL",
            position_snapshot={"symbol": symbol},
            research_context={},
            prompt_version="fixture",
            input_hash=f"{len(created):064x}",
            cache_hit=False,
            created_at=now,
        )
        db.add(row)
        db.flush()
        created.append(row.advice_id)
        return row

    monkeypatch.setattr("ashare_ai.api.app.create_manual_exit_advice", create_exit)
    monkeypatch.setattr("ashare_ai.api.app.enqueue_exit_advice", queued.append)
    try:
        client = TestClient(app)
        missing_key = client.post("/api/v1/exit-advice/manual", json={"symbol": "600519.SH"})
        assert missing_key.status_code == 400

        headers = {"Idempotency-Key": "manual-exit-once"}
        first = client.post(
            "/api/v1/exit-advice/manual",
            headers=headers,
            json={"symbol": "600519.SH"},
        )
        assert first.status_code == 202
        assert first.json()["status_url"].endswith(first.json()["advice_id"])
        replay = client.post(
            "/api/v1/exit-advice/manual",
            headers=headers,
            json={"symbol": "600519.SH"},
        )
        assert replay.status_code == 200
        assert replay.json()["advice_id"] == first.json()["advice_id"]
        assert len(created) == 1
        assert queued == [first.json()["advice_id"]]

        notices = NotificationService(session)
        first_notice = notices.create(
            user_id=alice.user_id,
            notification_type="STOP_LOSS_TRIGGERED",
            severity="CRITICAL",
            title="较早止损预警",
            body="纸面退出研究。",
            dedupe_key="alice-old",
            now=now,
        )
        notices.create(
            user_id=alice.user_id,
            notification_type="BUY_ENTRY_RANGE_HIT",
            severity="HIGH",
            title="较新买入区间提醒",
            body="仅模拟方案。",
            dedupe_key="alice-new",
            now=now + timedelta(minutes=1),
        )
        bob_notice = notices.create(
            user_id=bob.user_id,
            notification_type="STOP_LOSS_TRIGGERED",
            severity="CRITICAL",
            title="Bob 的止损预警",
            body="不应泄露。",
            dedupe_key="bob-private",
            now=now + timedelta(minutes=2),
        )
        session.commit()

        page = client.get("/api/v1/notifications", params={"limit": 1})
        assert page.status_code == 200
        assert page.json()["items"][0]["title"] == "较新买入区间提醒"
        assert page.json()["next_cursor"]
        second_page = client.get(
            "/api/v1/notifications",
            params={"limit": 1, "cursor": page.json()["next_cursor"]},
        )
        assert [item["notification_id"] for item in second_page.json()["items"]] == [
            first_notice.notification_id
        ]

        read = client.post(
            "/api/v1/notifications/read",
            headers={"Idempotency-Key": "notice-read-once"},
            json={"notification_ids": [first_notice.notification_id]},
        )
        assert read.status_code == 200
        assert read.json()["unread_count"] == 1
        assert client.post(
            "/api/v1/notifications/read",
            headers={"Idempotency-Key": "notice-read-once"},
            json={"notification_ids": [first_notice.notification_id]},
        ).json()["unread_count"] == 1

        contexts[0] = _context(bob)
        bob_items = client.get("/api/v1/notifications").json()["items"]
        assert [item["notification_id"] for item in bob_items] == [bob_notice.notification_id]
        assert first_notice.notification_id not in {
            item["notification_id"] for item in bob_items
        }
        bob_summary = client.post(
            "/api/v1/notifications/read",
            headers={"Idempotency-Key": "bob-cannot-read-alice"},
            json={"notification_ids": [first_notice.notification_id]},
        )
        assert bob_summary.status_code == 200
        assert bob_summary.json()["unread_count"] == 1
        assert bob_notice.notification_id in {
            item["notification_id"] for item in client.get("/api/v1/notifications").json()["items"]
        }
    finally:
        app.dependency_overrides.clear()
        session.close()
