from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ashare_ai.agents.model_settings import (
    ModelConfigurationService,
    ModelProbeResult,
    ModelSettingsDraft,
    ModelSettingsError,
)
from ashare_ai.api.app import app
from ashare_ai.api.auth import hash_password
from ashare_ai.api.dependencies import get_db
from ashare_ai.core.config import Settings
from ashare_ai.orchestration.production import ApplicationPipeline
from ashare_ai.storage.models import (
    Base,
    JobRun,
    ModelConfigurationVersion,
    UserAccount,
)


def _database() -> tuple[Session, sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    return factory(), factory


def _settings(key: str) -> Settings:
    return Settings(
        agent_backend="openai_compatible",
        llm_base_url="https://gateway.example/v1",
        llm_api_key="environment-secret",
        model_settings_encryption_keys=key,
    )


def test_model_configuration_is_encrypted_versioned_and_never_uses_plaintext() -> None:
    session, _ = _database()
    key = Fernet.generate_key().decode()
    service = ModelConfigurationService(_settings(key))
    probe = ModelProbeResult(
        reachable=True,
        message="ok",
        model="gpt-5.6-luna",
        checked_at=datetime.now(UTC),
    )
    first = service.save_and_activate(
        session,
        ModelSettingsDraft(
            base_url="https://gateway.example",
            api_key="database-secret",
        ),
        user_id=None,
        probe=probe,
    )
    session.commit()

    assert "database-secret" not in first.encrypted_api_key
    assert service.resolve(session).api_key == "database-secret"  # type: ignore[union-attr]
    second = service.save_and_activate(
        session,
        ModelSettingsDraft(
            base_url="https://gateway.example/v1",
            api_key=None,
            research_model="gpt-5.6-sol-2026-07",
        ),
        user_id=None,
        probe=probe,
    )
    session.commit()

    assert second.version == first.version + 1
    assert session.get(ModelConfigurationVersion, first.configuration_id).research_model == (
        "gpt-5.6-sol"
    )
    assert service.resolve(session).configuration_id == second.configuration_id  # type: ignore[union-attr]


def test_model_configuration_rejects_invalid_comma_hostname() -> None:
    session, _ = _database()
    service = ModelConfigurationService(_settings(Fernet.generate_key().decode()))
    with pytest.raises(ModelSettingsError, match="合法"):
        service.save_and_activate(
            session,
            ModelSettingsDraft(
                base_url="https://ccx,dengrb.top",
                api_key="secret",
            ),
            user_id=None,
            probe=None,
        )


def test_model_probe_checks_research_model_as_well_as_search_model(monkeypatch) -> None:
    session, _ = _database()
    service = ModelConfigurationService(_settings(Fernet.generate_key().decode()))
    attempted: list[tuple[str, str]] = []

    class Client:
        def __init__(self, *, model, reasoning_effort, **_kwargs) -> None:
            self.model = model
            self.reasoning_effort = reasoning_effort

        async def generate_structured(self, **_kwargs):
            attempted.append((self.model, self.reasoning_effort))
            return SimpleNamespace(output={"ok": True})

        async def probe_stream(self) -> bool:
            return False

    monkeypatch.setattr(
        "ashare_ai.agents.model_settings.OpenAICompatibleStructuredLLMClient", Client
    )

    result = asyncio.run(
        service.probe(
            ModelSettingsDraft(
                base_url="https://gateway.example/v1",
                api_key="database-secret",
                search_model="search-model",
                search_reasoning_effort="low",
                research_model="research-model",
                research_reasoning_effort="high",
            ),
            session,
        )
    )

    assert attempted == [("search-model", "low"), ("research-model", "high")]
    assert result.reachable is True


@pytest.mark.asyncio
@respx.mock
async def test_model_settings_probe_can_skip_stream_probe() -> None:
    route = respx.post("https://gateway.example/v1/responses").mock(
        return_value=httpx.Response(200, json={"model": "gpt-test", "output_text": '{"ok": true}'})
    )
    session, _ = _database()
    service = ModelConfigurationService(_settings(Fernet.generate_key().decode()))

    result = await service.probe(
        ModelSettingsDraft(
            base_url="https://gateway.example",
            api_key="probe-secret",
            enabled=True,
            search_model="gpt-test",
            search_reasoning_effort="high",
            research_model="gpt-test",
            research_reasoning_effort="high",
        ),
        session,
        include_streaming=False,
    )

    assert route.call_count == 1
    assert result.structured_output_supported is True
    assert result.streaming_checked is False


def test_research_manifest_pins_active_model_revision(monkeypatch, tmp_path) -> None:
    session, factory = _database()
    settings = _settings(Fernet.generate_key().decode())
    settings.policy_config_path = tmp_path / "policy.json"
    settings.policy_config_path.write_text("{}", encoding="utf-8")
    service = ModelConfigurationService(settings)
    row = service.save_and_activate(
        session,
        ModelSettingsDraft(
            base_url="https://gateway.example/v1",
            api_key="database-secret",
        ),
        user_id=None,
        probe=None,
    )
    session.commit()
    session.close()

    class Backend:
        def run_manifest(self, trading_date, decision_at):
            return {
                "trading_date": trading_date.isoformat(),
                "decision_at": decision_at.isoformat(),
            }

    monkeypatch.setattr("ashare_ai.orchestration.production.get_settings", lambda: settings)
    pipeline = ApplicationPipeline(Backend(), session_factory=factory)  # type: ignore[arg-type]
    run_id = pipeline.start_run(date(2026, 7, 14))

    with factory() as check:
        run = check.get(JobRun, run_id)
        assert run is not None
        reference = run.manifest["model_configuration"]
        assert reference["configuration_id"] == row.configuration_id
        assert reference["version"] == row.version
        assert reference["config_sha256"] == row.config_sha256


def test_admin_model_settings_api_requires_csrf_and_never_returns_secret(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        session.add(
            UserAccount(
                username="admin-test",
                password_hash=hash_password("admin-password"),
                role="ADMIN",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    settings = _settings(Fernet.generate_key().decode())
    monkeypatch.setattr("ashare_ai.agents.model_settings.get_settings", lambda: settings)

    async def probe(self, draft, session):
        del self, draft, session
        return ModelProbeResult(
            reachable=True,
            message="ok",
            model="gpt-5.6-luna",
            checked_at=datetime.now(UTC),
        )

    monkeypatch.setattr(ModelConfigurationService, "probe", probe)

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        anonymous = TestClient(app)
        assert anonymous.get("/api/v1/admin/model-settings").status_code == 401
        client = TestClient(app)
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin-test", "password": "admin-password"},
        ).status_code == 200
        payload = {
            "base_url": "https://gateway.example/v1",
            "api_key": "must-never-be-returned",
            "search_model": "gpt-5.6-luna",
            "search_reasoning_effort": "low",
            "research_model": "gpt-5.6-sol",
            "research_reasoning_effort": "high",
            "timeout_seconds": 90,
            "enabled": True,
        }
        assert client.put("/api/v1/admin/model-settings", json=payload).status_code == 403
        csrf = client.cookies.get("ashare_csrf")
        response = client.put(
            "/api/v1/admin/model-settings",
            json=payload,
            headers={"x-csrf-token": csrf},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reachable"] is True
        assert body["api_key_configured"] is True
        assert "must-never-be-returned" not in response.text
    finally:
        app.dependency_overrides.clear()
