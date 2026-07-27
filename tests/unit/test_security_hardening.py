from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from ashare_ai.agents.model_settings import ModelSettingsError, normalize_base_url
from ashare_ai.api.auth import _auth_source
from ashare_ai.core.config import Settings
from ashare_ai.core.security import safe_error_message, safe_error_text
from ashare_ai.storage.objects import LocalObjectStore


def _production_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "cookie_secure": True,
        "trusted_hosts": "research.example.com",
        "admin_username": "admin",
        "admin_password": "a-strong-admin-password",
        "database_url": (
            "postgresql+psycopg://ashare:a-strong-database-password@postgres:5432/ashare"
        ),
        "redis_url": "redis://:a-strong-redis-password@redis:6379/0",
        "object_store_endpoint": None,
        "object_store_access_key": None,
        "object_store_secret_key": None,
        "agent_backend": "builtin",
        "model_allowed_hosts": "gateway.example.com",
        "allow_demo_data": False,
        "canonical_bundle_mode": "akshare",
        "personal_data_encryption_keys": Fernet.generate_key().decode(),
    }
    values.update(changes)
    return Settings(**values)


def test_production_configuration_fails_closed_and_accepts_hardened_values() -> None:
    _production_settings().validate_production_security()
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        _production_settings(cookie_secure=False).validate_production_security()
    with pytest.raises(ValueError, match="Redis"):
        _production_settings(redis_url="redis://redis:6379/0").validate_production_security()
    with pytest.raises(ValueError, match="default database"):
        _production_settings(
            database_url="postgresql+psycopg://ashare:ashare@postgres:5432/ashare"
        ).validate_production_security()
    with pytest.raises(ValueError, match="object-store endpoints must use HTTPS"):
        _production_settings(
            object_store_endpoint="http://object-store:9000",
            object_store_access_key="access-key",
            object_store_secret_key="a-strong-object-secret",
        ).validate_production_security()
    with pytest.raises(ValueError, match="PERSONAL_DATA_ENCRYPTION_KEYS"):
        _production_settings(
            personal_data_encryption_keys=None,
            model_settings_encryption_keys=None,
        ).validate_production_security()


def test_production_model_gateway_requires_https_and_an_explicit_host_allowlist() -> None:
    settings = _production_settings()
    assert normalize_base_url("https://gateway.example.com", settings) == (
        "https://gateway.example.com/v1"
    )
    with pytest.raises(ModelSettingsError, match="HTTPS"):
        normalize_base_url("http://gateway.example.com", settings)
    with pytest.raises(ModelSettingsError, match="白名单"):
        normalize_base_url("https://metadata.example.net", settings)
    with pytest.raises(ModelSettingsError, match="合法"):
        normalize_base_url("https://user:password@gateway.example.com", settings)


def test_proxy_source_uses_rightmost_address_not_spoofable_first_forwarded_value() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [(b"x-forwarded-for", b"203.0.113.99, 198.51.100.8")],
            "client": ("172.20.0.5", 12345),
            "server": ("api", 8000),
            "scheme": "http",
        }
    )
    assert _auth_source(request) == "198.51.100.8"


def test_error_messages_redact_credentials_endpoints_and_server_paths() -> None:
    error = RuntimeError(
        "Authorization: Bearer top-secret at https://internal.example/v1 "
        "from C:\\service\\private\\config.json"
    )
    message = safe_error_message(error)
    assert "top-secret" not in message
    assert "internal.example" not in message
    assert "private" not in message
    assert "[REDACTED]" in message

    database_message = safe_error_text(
        '(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint '
        '"uq_snapshot_versioned_hash" [SQL: INSERT INTO snapshot_manifests ...]'
    )
    assert database_message == "数据库操作失败，未写入不完整结果"
    assert "snapshot_manifests" not in database_message


def test_local_object_store_rejects_content_named_file_outside_its_root(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    payload = b"outside"
    from ashare_ai.core.hashing import sha256_bytes

    outside = tmp_path / sha256_bytes(payload)
    outside.write_bytes(payload)
    with pytest.raises(ValueError, match="outside"):
        store.get(outside.as_uri())
