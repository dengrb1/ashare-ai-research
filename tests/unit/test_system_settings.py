from __future__ import annotations

from datetime import UTC, datetime

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import _system_settings_restart_command, app
from ashare_ai.api.auth import hash_password
from ashare_ai.api.dependencies import get_db
from ashare_ai.core.config import Settings
from ashare_ai.core.system_settings import SystemConfigurationService, SystemSettingsError
from ashare_ai.storage.models import Base, SystemConfigurationVersion, UserAccount


class _UnlockRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, *, ex: int) -> None:
        assert ex == 600
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)


def _service() -> tuple[Session, SystemConfigurationService]:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    settings = Settings(
        database_url="sqlite+pysqlite://",
        redis_url="redis://localhost:6379/0",
        model_settings_encryption_keys=Fernet.generate_key().decode(),
        tushare_token="environment-tushare",
        market_cache_seconds=15,
    )
    return session, SystemConfigurationService(settings)


def test_system_settings_version_overrides_restore_and_never_expose_secret() -> None:
    session, service = _service()
    saved = service.save(
        session,
        public_updates={"market_cache_seconds": 25},
        secret_updates={"tushare_token": "database-secret"},
        user_id=None,
    )
    session.commit()

    row = session.get(SystemConfigurationVersion, saved.configuration_id)
    assert row is not None
    assert row.version == 1
    assert "database-secret" not in (row.encrypted_secret_values or "")
    assert service.resolve(session).settings.tushare_token == "database-secret"
    view = service.public_view(session)
    assert view["values"]["market_cache_seconds"] == 25
    assert view["sources"]["market_cache_seconds"] == "database"
    assert view["secret_configured"]["tushare_token"] is True
    assert "database-secret" not in str(view)

    restored = service.restore_field(session, field="market_cache_seconds", user_id=None)
    session.commit()
    assert restored.version == 2
    assert restored.settings.market_cache_seconds == 15
    assert restored.settings.tushare_token == "database-secret"

    all_restored = service.restore_all(session, user_id=None)
    session.commit()
    assert all_restored.version == 3
    assert all_restored.settings.tushare_token == "environment-tushare"


def test_dual_mode_rejects_insufficient_gateway_capacity(monkeypatch) -> None:
    session, service = _service()
    service.settings.model_gateway_max_concurrency = 7

    try:
        service.save(
            session,
            public_updates={"research_execution_mode": "DUAL", "llm_agent_max_concurrency": 4},
            secret_updates={},
            user_id=None,
        )
    except SystemSettingsError as error:
        assert "MODEL_GATEWAY_MAX_CONCURRENCY" in str(error)
    else:
        raise AssertionError("DUAL mode must fail closed when gateway capacity is insufficient")


def test_system_settings_restart_command_matches_saved_execution_mode() -> None:
    assert _system_settings_restart_command("SERIAL") == (
        "docker compose -p ashare-ai-src -f compose.yaml --profile dual-research "
        "stop research-worker; docker compose -p ashare-ai-src -f compose.yaml "
        "up -d --force-recreate job-worker"
    )
    assert _system_settings_restart_command("DUAL") == (
        "docker compose -p ashare-ai-src -f compose.yaml --profile dual-research "
        "up -d --force-recreate job-worker research-worker"
    )


def test_system_settings_api_requires_admin_csrf_and_is_idempotent(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        session.add_all(
            [
                UserAccount(
                    username="system-admin",
                    password_hash=hash_password("system-admin-password"),
                    role="ADMIN",
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                ),
                UserAccount(
                    username="system-user",
                    password_hash=hash_password("system-user-password"),
                    role="USER",
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                ),
                UserAccount(
                    username="system-admin-two",
                    password_hash=hash_password("system-admin-two-password"),
                    role="ADMIN",
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.commit()
    settings = Settings(model_settings_encryption_keys=Fernet.generate_key().decode())
    monkeypatch.setattr("ashare_ai.core.system_settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "ashare_ai.api.app._system_worker_snapshot", lambda _hash: ("SERIAL", False, [], {})
    )
    unlock_redis = _UnlockRedis()
    monkeypatch.setattr(
        "ashare_ai.api.system_settings_unlock._redis_client", lambda: unlock_redis
    )

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        anonymous = TestClient(app)
        assert anonymous.get("/api/v1/admin/system-settings").status_code == 401
        assert anonymous.get("/api/v1/admin/system-resources").status_code == 401
        user = TestClient(app)
        assert user.post(
            "/api/v1/auth/login",
            json={"username": "system-user", "password": "system-user-password"},
        ).status_code == 200
        assert user.get("/api/v1/admin/system-settings").status_code == 403
        assert user.get("/api/v1/admin/system-resources").status_code == 403

        client = TestClient(app)
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "system-admin", "password": "system-admin-password"},
        ).status_code == 200
        resources = client.get("/api/v1/admin/system-resources")
        assert resources.status_code == 200
        assert resources.json()["scope"] in {"HOST", "CONTAINER"}
        assert "redis_url" not in resources.text
        payload = {"market_cache_seconds": 23, "tushare_token": "must-not-return"}
        assert client.put("/api/v1/admin/system-settings", json=payload).status_code == 403
        csrf = client.cookies.get("ashare_csrf")
        headers = {"x-csrf-token": csrf, "Idempotency-Key": "system-settings-once"}
        assert client.put(
            "/api/v1/admin/system-settings", json=payload, headers=headers
        ).status_code == 403
        unlocked = client.post(
            "/api/v1/admin/system-settings/unlock",
            json={"password": "system-admin-password"},
            headers={"x-csrf-token": csrf},
        )
        assert unlocked.status_code == 200
        headers["X-System-Settings-Unlock"] = unlocked.json()["unlock_token"]

        other_session = TestClient(app)
        assert other_session.post(
            "/api/v1/auth/login",
            json={"username": "system-admin", "password": "system-admin-password"},
        ).status_code == 200
        other_csrf = other_session.cookies.get("ashare_csrf")
        assert other_session.put(
            "/api/v1/admin/system-settings",
            json={"market_cache_seconds": 22},
            headers={
                "x-csrf-token": other_csrf,
                "X-System-Settings-Unlock": unlocked.json()["unlock_token"],
            },
        ).status_code == 403

        second_admin = TestClient(app)
        assert second_admin.post(
            "/api/v1/auth/login",
            json={
                "username": "system-admin-two",
                "password": "system-admin-two-password",
            },
        ).status_code == 200
        second_csrf = second_admin.cookies.get("ashare_csrf")
        assert second_admin.post(
            "/api/v1/admin/system-settings/unlock",
            json={"password": "system-admin-password"},
            headers={"x-csrf-token": second_csrf},
        ).status_code == 401
        assert second_admin.post(
            "/api/v1/admin/system-settings/unlock",
            json={"password": "system-admin-two-password"},
            headers={"x-csrf-token": second_csrf},
        ).status_code == 200

        first = client.put("/api/v1/admin/system-settings", json=payload, headers=headers)
        second = client.put("/api/v1/admin/system-settings", json=payload, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["version"] == second.json()["version"] == 1
        assert first.json()["secret_configured"]["tushare_token"] is True
        assert "must-not-return" not in first.text
        assert client.put(
            "/api/v1/admin/system-settings",
            json={"market_cache_seconds": 24},
            headers=headers,
        ).status_code == 409
        restore_headers = {
            "x-csrf-token": csrf,
            "X-System-Settings-Unlock": unlocked.json()["unlock_token"],
        }
        assert client.delete(
            "/api/v1/admin/system-settings/market_cache_seconds",
            headers=restore_headers,
        ).status_code == 200
        assert client.delete(
            "/api/v1/admin/system-settings", headers=restore_headers
        ).status_code == 200
        unlock_redis.values.clear()
        assert client.delete(
            "/api/v1/admin/system-settings", headers=restore_headers
        ).status_code == 403
    finally:
        app.dependency_overrides.clear()
