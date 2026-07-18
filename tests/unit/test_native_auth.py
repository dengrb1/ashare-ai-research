from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import hash_password
from ashare_ai.api.dependencies import get_db
from ashare_ai.storage.models import Base, UserAccount, UserSession


def test_native_bearer_refresh_rotation_revoke_expiry_and_disable() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    user = UserAccount(
        username="native-user",
        password_hash=hash_password("native-password"),
        role="USER",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        issued = client.post(
            "/api/v1/auth/token",
            json={"username": "native-user", "password": "native-password"},
        )
        assert issued.status_code == 200
        first = issued.json()
        assert first["token_type"] == "bearer"
        assert first["expires_in"] == 15 * 60
        stored = session.query(UserSession).one()
        assert stored.session_type == "APP"
        assert stored.token_hash != first["access_token"]
        assert stored.refresh_token_hash != first["refresh_token"]

        headers = {"Authorization": f"Bearer {first['access_token']}"}
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
        assert client.put(
            "/api/v1/assets",
            headers=headers,
            json={"watchlist": [], "positions": []},
        ).status_code == 200

        rotated = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert rotated.status_code == 200
        second = rotated.json()
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
        assert client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        ).status_code == 401

        second_headers = {"Authorization": f"Bearer {second['access_token']}"}
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        assert client.get("/api/v1/auth/me", headers=second_headers).status_code == 401
        stored.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        session.commit()
        assert client.post(
            "/api/v1/auth/revoke", json={"refresh_token": second["refresh_token"]}
        ).status_code == 204
        assert client.get("/api/v1/auth/me", headers=second_headers).status_code == 401

        replacement = client.post(
            "/api/v1/auth/token",
            json={"username": "native-user", "password": "native-password"},
        ).json()
        user.enabled = False
        session.commit()
        assert client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {replacement['access_token']}"},
        ).status_code == 401
    finally:
        app.dependency_overrides.clear()
        session.close()
