from __future__ import annotations

from datetime import UTC, datetime

import pytest
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


class _NoopMarketDataService:
    def start(self) -> bool:
        return True


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
        edge_gateway_enabled=False,
        edge_domain=None,
        edge_acme_email=None,
        edge_acme_ca_server="letsencrypt",
        edge_frpc_enabled=False,
        edge_frpc_config_file="./docker/edge-gateway/frpc.disabled.toml",
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
    assert view["values"]["market_hedge_delay_seconds"] == 0.5
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
    assert _system_settings_restart_command("SERIAL", True).endswith(
        "docker compose -p ashare-ai-src -f compose.yaml --profile edge up -d "
        "--force-recreate edge-gateway"
    )


def test_system_settings_keeps_edge_gateway_disabled_by_default() -> None:
    session, service = _service()
    assert service.resolve(session).settings.edge_gateway_enabled is False
    saved = service.save(
        session,
        public_updates={
            "edge_gateway_enabled": True,
            "edge_domain": "research.example.com",
            "edge_acme_email": "ops@example.com",
        },
        secret_updates={},
        user_id=None,
    )
    assert saved.settings.edge_gateway_enabled is True
    assert service.public_view(session)["sources"]["edge_gateway_enabled"] == "database"


def test_edge_gateway_config_is_versioned_and_restorable() -> None:
    session, service = _service()
    saved = service.save(
        session,
        public_updates={
            "edge_gateway_enabled": True,
            "edge_domain": "research.example.com",
            "edge_acme_email": "ops@example.com",
            "edge_acme_ca_server": "zerossl",
            "edge_frpc_enabled": False,
            "edge_frpc_config_file": "./.secrets/edge-frpc.toml",
        },
        secret_updates={},
        user_id=None,
    )
    session.commit()
    assert saved.settings.edge_domain == "research.example.com"
    assert saved.settings.edge_acme_email == "ops@example.com"
    assert saved.settings.edge_acme_ca_server == "zerossl"
    assert saved.settings.edge_frpc_config_file == "./.secrets/edge-frpc.toml"
    view = service.public_view(session)
    assert view["values"]["edge_domain"] == "research.example.com"
    assert view["sources"]["edge_domain"] == "database"
    assert view["sources"]["edge_frpc_config_file"] == "database"

    # Restoring a required field while the gateway stays enabled fails closed,
    # because the container could no longer boot without a public domain.
    with pytest.raises(SystemSettingsError, match="edge gateway"):
        service.restore_field(session, field="edge_domain", user_id=None)
    session.rollback()

    restored = service.restore_field(session, field="edge_acme_ca_server", user_id=None)
    session.commit()
    assert restored.settings.edge_acme_ca_server == "letsencrypt"
    assert service.public_view(session)["sources"]["edge_acme_ca_server"] == "environment"

    disabled = service.save(
        session,
        public_updates={"edge_gateway_enabled": False},
        secret_updates={},
        user_id=None,
    )
    session.commit()
    assert disabled.settings.edge_gateway_enabled is False
    restored = service.restore_field(session, field="edge_domain", user_id=None)
    session.commit()
    assert restored.settings.edge_domain is None
    assert service.public_view(session)["sources"]["edge_domain"] == "environment"


def test_edge_gateway_enable_requires_domain_and_acme_email() -> None:
    session, service = _service()
    for update in (
        {"edge_gateway_enabled": True},
        {"edge_gateway_enabled": True, "edge_domain": "research.example.com"},
    ):
        try:
            service.save(session, public_updates=update, secret_updates={}, user_id=None)
        except SystemSettingsError as error:
            assert "edge gateway" in str(error)
        else:
            raise AssertionError("enabling the edge gateway without required values must fail")

    with pytest.raises(SystemSettingsError, match="DNS host name"):
        service.save(
            session,
            public_updates={
                "edge_gateway_enabled": True,
                "edge_domain": "https://research.example.com",
                "edge_acme_email": "ops@example.com",
            },
            secret_updates={},
            user_id=None,
        )


def test_edge_gateway_frpc_enable_requires_config_file_path() -> None:
    session, service = _service()
    with pytest.raises(SystemSettingsError, match="frpc"):
        service.save(
            session,
            public_updates={
                "edge_gateway_enabled": True,
                "edge_domain": "research.example.com",
                "edge_acme_email": "ops@example.com",
                "edge_frpc_enabled": True,
                "edge_frpc_config_file": "",
            },
            secret_updates={},
            user_id=None,
        )


def test_topology_controller_endpoint_requires_its_private_capability(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    settings = Settings(
        topology_controller_token="x" * 32,
        edge_gateway_enabled=False,
        edge_domain=None,
        edge_acme_email=None,
        edge_acme_ca_server="letsencrypt",
        edge_frpc_enabled=False,
        edge_frpc_config_file="./docker/edge-gateway/frpc.disabled.toml",
    )
    monkeypatch.setattr("ashare_ai.api.app.get_settings", lambda: settings)
    monkeypatch.setattr("ashare_ai.core.system_settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "ashare_ai.api.app._system_worker_snapshot", lambda _hash: ("SERIAL", False, [], {})
    )

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        assert client.get("/api/internal/topology-desired").status_code == 403
        response = client.get(
            "/api/internal/topology-desired", headers={"X-Topology-Controller-Token": "x" * 32}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["research_execution_mode"] == "SERIAL"
        assert body["edge_gateway_enabled"] is False
        assert body["edge_domain"] is None
        assert body["edge_acme_email"] is None
        assert body["edge_acme_ca_server"] == "letsencrypt"
        assert body["edge_frpc_enabled"] is False
        assert body["edge_frpc_config_file"] == "./docker/edge-gateway/frpc.disabled.toml"
        assert body["auto_restart_enabled"] is False
        assert body["restart_required"] is False
        assert isinstance(body["topology_sha256"], str) and body["topology_sha256"]
    finally:
        app.dependency_overrides.clear()


def test_topology_controller_endpoint_returns_persisted_edge_config(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    settings = Settings(
        topology_controller_token="x" * 32,
        edge_gateway_enabled=False,
        edge_domain=None,
        edge_acme_email=None,
        edge_acme_ca_server="letsencrypt",
        edge_frpc_enabled=False,
        edge_frpc_config_file="./docker/edge-gateway/frpc.disabled.toml",
    )
    monkeypatch.setattr("ashare_ai.api.app.get_settings", lambda: settings)
    monkeypatch.setattr("ashare_ai.core.system_settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "ashare_ai.api.app._system_worker_snapshot", lambda _hash: ("SERIAL", False, [], {})
    )
    with factory() as session:
        SystemConfigurationService(settings).save(
            session,
            public_updates={
                "edge_gateway_enabled": True,
                "edge_domain": "research.example.com",
                "edge_acme_email": "ops@example.com",
                "edge_frpc_enabled": True,
                "edge_frpc_config_file": "./.secrets/edge-frpc.toml",
            },
            secret_updates={},
            user_id=None,
        )
        session.commit()

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/internal/topology-desired", headers={"X-Topology-Controller-Token": "x" * 32}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["edge_gateway_enabled"] is True
        assert body["edge_domain"] == "research.example.com"
        assert body["edge_acme_email"] == "ops@example.com"
        assert body["edge_frpc_enabled"] is True
        assert body["edge_frpc_config_file"] == "./.secrets/edge-frpc.toml"
    finally:
        app.dependency_overrides.clear()


def test_auto_restart_enabled_is_versioned_and_restorable() -> None:
    session, service = _service()
    assert service.resolve(session).settings.auto_restart_enabled is False
    saved = service.save(
        session,
        public_updates={"auto_restart_enabled": True},
        secret_updates={},
        user_id=None,
    )
    assert saved.settings.auto_restart_enabled is True
    assert service.public_view(session)["sources"]["auto_restart_enabled"] == "database"
    restored = service.restore_field(session, field="auto_restart_enabled", user_id=None)
    session.commit()
    assert restored.settings.auto_restart_enabled is False
    assert service.public_view(session)["sources"]["auto_restart_enabled"] == "environment"


def test_system_settings_api_requires_admin_csrf_and_is_idempotent(
    monkeypatch, tmp_path
) -> None:
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
    monkeypatch.setattr(
        "ashare_ai.api.app.get_market_data_service", lambda: _NoopMarketDataService()
    )
    monkeypatch.setattr("ashare_ai.api.app.reset_market_data_service", lambda: None)
    monkeypatch.setattr("ashare_ai.api.app._native_runtime_root", lambda: tmp_path)
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
        runtime_mode = client.get("/api/v1/runtime/mode")
        assert runtime_mode.status_code == 200
        assert runtime_mode.json()["mode"] == "LIGHTWEIGHT"
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
        mode_headers = {
            "x-csrf-token": csrf,
            "X-System-Settings-Unlock": unlocked.json()["unlock_token"],
            "Idempotency-Key": "runtime-mode-once",
        }
        mode_response = client.put(
            "/api/v1/admin/runtime/mode",
            json={"mode": "SUPREME"},
            headers=mode_headers,
        )
        assert mode_response.status_code == 200
        assert mode_response.json()["mode"] == "SUPREME"
        assert mode_response.json()["memory_strategy"] == "MAX_THROUGHPUT"
        assert client.put(
            "/api/v1/admin/runtime/mode",
            json={"mode": "SUPREME"},
            headers=mode_headers,
        ).json()["mode"] == "SUPREME"
        identity_headers = {
            "x-csrf-token": csrf,
            "X-System-Settings-Unlock": unlocked.json()["unlock_token"],
            "Idempotency-Key": "runtime-identity-once",
        }
        assert client.put(
            "/api/v1/admin/runtime-identity",
            json={"mode": "task"},
            headers={"x-csrf-token": csrf},
        ).status_code == 403
        assert client.put(
            "/api/v1/admin/runtime-identity",
            json={"mode": "system"},
            headers=identity_headers,
        ).status_code == 422
        first_identity = client.put(
            "/api/v1/admin/runtime-identity",
            json={"mode": "account"},
            headers=identity_headers,
        )
        second_identity = client.put(
            "/api/v1/admin/runtime-identity",
            json={"mode": "account"},
            headers=identity_headers,
        )
        assert first_identity.status_code == second_identity.status_code == 200
        assert first_identity.json()["mode"] == second_identity.json()["mode"] == "account"
        assert first_identity.json()["supported_modes"] == ["task", "account"]
        assert '"mode": "account"' in (
            tmp_path / "config" / "runtime-identity.json"
        ).read_text(encoding="utf-8")
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
