from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api.auth import AuthContext
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.storage.models import (
    AIChatThread,
    Base,
    PersonalArchiveJob,
    UserAccount,
    UserSession,
)

app_module = import_module("ashare_ai.api.app")


def _client() -> tuple[TestClient, Session, UserAccount]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    user = UserAccount(
        username="owner",
        password_hash="hash",
        role="USER",
        enabled=True,
        session_version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.flush()
    auth_session = UserSession(
        user_id=user.user_id,
        token_hash="token-hash",
        csrf_hash="csrf-hash",
        session_type="WEB",
        user_session_version=1,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=1),
    )
    session.add(auth_session)
    session.commit()
    context = AuthContext(user=user, session=auth_session)

    def override_db():
        yield session

    app_module.app.dependency_overrides[get_db] = override_db
    app_module.app.dependency_overrides[get_auth_context] = lambda: context
    app_module.app.dependency_overrides[get_write_context] = lambda: context
    return TestClient(app_module.app), session, user


def _clear_overrides() -> None:
    app_module.app.dependency_overrides.clear()


def test_thread_index_management_cursor_archive_and_bulk_delete() -> None:
    client, session, user = _client()
    try:
        created = [
            client.post("/api/v1/ai/chat/threads", json={"title": f"thread-{index}"}).json()
            for index in range(3)
        ]
        pinned = client.patch(
            f"/api/v1/ai/chat/threads/{created[0]['thread_id']}",
            json={"pinned": True, "group_label": "我的分组"},
        )
        assert pinned.status_code == 200
        assert pinned.json()["group_mode"] == "MANUAL"

        first = client.get("/api/v1/ai/chat/thread-index", params={"limit": 2})
        assert first.status_code == 200
        assert first.json()["items"][0]["thread_id"] == created[0]["thread_id"]
        assert first.json()["next_cursor"]
        second = client.get(
            "/api/v1/ai/chat/thread-index",
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )
        assert second.status_code == 200
        assert {item["thread_id"] for item in first.json()["items"]}.isdisjoint(
            {item["thread_id"] for item in second.json()["items"]}
        )

        archived = client.patch(
            f"/api/v1/ai/chat/threads/{created[1]['thread_id']}", json={"archived": True}
        )
        assert archived.json()["archived_at"]
        assert all(
            item["thread_id"] != created[1]["thread_id"]
            for item in client.get("/api/v1/ai/chat/threads").json()
        )
        assert client.get(
            "/api/v1/ai/chat/thread-index", params={"archived": "true"}
        ).json()["items"][0]["thread_id"] == created[1]["thread_id"]

        deleted = client.post(
            "/api/v1/ai/chat/threads:bulk-delete",
            json={"thread_ids": [created[0]["thread_id"], created[2]["thread_id"]]},
        )
        assert deleted.json() == {"deleted": 2}
        assert session.query(AIChatThread).filter_by(user_id=user.user_id).count() == 1
    finally:
        _clear_overrides()
        session.close()


def test_personal_archive_apply_is_idempotent_and_owner_scoped(monkeypatch) -> None:
    client, session, user = _client()
    now = datetime.now(UTC)
    preview = PersonalArchiveJob(
        user_id=user.user_id,
        kind="IMPORT_PREVIEW",
        status="SUCCEEDED",
        phase="COMPLETED",
        progress=100,
        encrypted_secret="wrapped-secret",
        source_object_uri="file:///private/source.ashare",
        result={"watchlist": {}},
        created_at=now,
        completed_at=now,
        expires_at=now + timedelta(hours=24),
    )
    session.add(preview)
    session.commit()
    monkeypatch.setattr(app_module, "enqueue_personal_archive", lambda _archive_id: None)
    headers = {"Idempotency-Key": "apply-once"}
    try:
        first = client.post(
            f"/api/v1/me/data-imports/{preview.archive_id}/apply",
            headers=headers,
            json={"merge_options": {"total_assets": "CURRENT"}},
        )
        assert first.status_code == 202
        replay = client.post(
            f"/api/v1/me/data-imports/{preview.archive_id}/apply",
            headers=headers,
            json={"merge_options": {"total_assets": "CURRENT"}},
        )
        assert replay.status_code == 200
        assert replay.json()["archive_id"] == first.json()["archive_id"]
        conflict = client.post(
            f"/api/v1/me/data-imports/{preview.archive_id}/apply",
            headers=headers,
            json={"merge_options": {"total_assets": "IMPORTED"}},
        )
        assert conflict.status_code == 409

        other = UserAccount(
            username="other",
            password_hash="hash",
            role="USER",
            enabled=True,
            session_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(other)
        session.commit()
        assert session.get(PersonalArchiveJob, first.json()["archive_id"]).user_id == user.user_id
    finally:
        _clear_overrides()
        session.close()
