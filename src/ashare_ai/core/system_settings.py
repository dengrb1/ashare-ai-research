"""Versioned operational settings and worker-topology runtime resolution.

The environment remains the deployment baseline.  This module stores only
administrator overrides, so removing one override reliably returns the process
to its Compose/.env value without copying deployment secrets into PostgreSQL.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.storage.models import (
    ActiveSystemConfiguration,
    BacktestRun,
    JobRun,
    SystemConfigurationVersion,
)

ExecutionMode = Literal["SERIAL", "DUAL"]

# Keep this explicit allow-list close to Settings.  Database, Redis, auth,
# encryption, policies and filesystem topology deliberately never appear here.
PUBLIC_SETTING_FIELDS = frozenset(
    {
        "research_execution_mode",
        "llm_agent_max_concurrency",
        "object_store_endpoint",
        "object_store_bucket",
        "object_store_secure",
        "searxng_base_url",
        "searxng_timeout_seconds",
        "searxng_max_results",
        "market_cache_seconds",
        "market_kline_cache_seconds",
        "market_prefetch_max_workers",
        "market_provider_max_workers",
        "market_provider_max_queue",
        "market_cache_max_entries",
        "market_stale_seconds",
        "market_timeout_seconds",
        "financial_search_cache_seconds",
        "financial_search_max_concurrency",
        "financial_search_rate_limit_per_minute",
        "daily_research_start_hour",
        "daily_research_start_minute",
        "daily_research_retry_minutes",
        "daily_research_retry_limit_minutes",
        "worker_lease_seconds",
        "canonical_bundle_mode",
        "allow_demo_data",
        "akshare_bundle_size",
        "akshare_history_sessions",
        "akshare_fetch_max_attempts",
        "akshare_fetch_backoff_seconds",
        "minimum_listing_days",
        "minimum_median_amount",
    }
)
SECRET_SETTING_FIELDS = frozenset(
    {
        "tushare_token",
        "object_store_access_key",
        "object_store_secret_key",
    }
)
TOPOLOGY_FIELDS = frozenset({"research_execution_mode", "llm_agent_max_concurrency"})
_ACTIVE_RESEARCH_STATUSES = frozenset(
    {"PENDING", "QUEUED", "RUNNING", "PROCESSING", "DATA_READINESS_WAITING", "CANCEL_REQUESTED"}
)
_ACTIVE_BACKTEST_STATUSES = frozenset(
    {"PENDING", "QUEUED", "RUNNING", "PROCESSING", "CANCEL_REQUESTED"}
)


class SystemSettingsError(RuntimeError):
    """Validation or encryption failure for administrator system settings."""


@dataclass(frozen=True)
class SystemRuntimeSettings:
    settings: Settings
    source: Literal["database", "environment"]
    configuration_id: str | None
    version: int
    config_sha256: str
    overridden_public_fields: frozenset[str]
    overridden_secret_fields: frozenset[str]

    @property
    def execution_mode(self) -> ExecutionMode:
        return self.settings.research_execution_mode

    @property
    def topology_sha256(self) -> str:
        return stable_hash(
            {
                "research_execution_mode": self.settings.research_execution_mode,
                "llm_agent_max_concurrency": self.settings.llm_agent_max_concurrency,
            }
        )

    def manifest_reference(self) -> dict[str, object]:
        return {
            "source": self.source,
            "configuration_id": self.configuration_id,
            "version": self.version,
            "config_sha256": self.config_sha256,
            "topology_sha256": self.topology_sha256,
        }


def _memory_available_bytes() -> int:
    """Return host available RAM without adding a platform-specific dependency."""

    try:
        import psutil  # type: ignore[import-untyped]

        return int(psutil.virtual_memory().available)
    except (ImportError, OSError):
        pass
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


class SystemConfigurationService:
    scope = "default"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def resolve(self, session: Session) -> SystemRuntimeSettings:
        pointer = session.get(ActiveSystemConfiguration, self.scope)
        if pointer is None:
            return self._environment_runtime()
        row = session.get(SystemConfigurationVersion, pointer.configuration_id)
        if row is None:
            raise SystemSettingsError("active system configuration is missing")
        public_values = dict(row.public_values or {})
        self._ensure_allowed(public_values, PUBLIC_SETTING_FIELDS)
        secret_values = self._decrypt_values(row) if row.encrypted_secret_values else {}
        values = self._validated_settings({**public_values, **secret_values})
        return SystemRuntimeSettings(
            settings=values,
            source="database",
            configuration_id=row.configuration_id,
            version=row.version,
            config_sha256=row.config_sha256,
            overridden_public_fields=frozenset(public_values),
            overridden_secret_fields=frozenset(secret_values),
        )

    def save(
        self,
        session: Session,
        *,
        public_updates: dict[str, Any],
        secret_updates: dict[str, str],
        user_id: str | None,
    ) -> SystemRuntimeSettings:
        self._ensure_allowed(public_updates, PUBLIC_SETTING_FIELDS)
        self._ensure_allowed(secret_updates, SECRET_SETTING_FIELDS)
        current = self.resolve(session)
        current_row = (
            session.get(SystemConfigurationVersion, current.configuration_id)
            if current.configuration_id
            else None
        )
        public_values = dict(current_row.public_values or {}) if current_row else {}
        public_values.update(public_updates)

        # Existing ciphertext can be copied when only public fields change;
        # decryption is only required when a secret itself is edited.
        encrypted_secrets = current_row.encrypted_secret_values if current_row else None
        encryption_key_id = current_row.encryption_key_id if current_row else None
        if secret_updates:
            secret_values = self._decrypt_values(current_row) if current_row else {}
            secret_values.update(secret_updates)
            encrypted_secrets, encryption_key_id = self._encrypt_values(secret_values)
        else:
            secret_values = (
                self._decrypt_values(current_row)
                if current_row and current_row.encrypted_secret_values
                else {}
            )

        effective = self._validated_settings({**public_values, **secret_values})
        self._validate_transition(session, current, effective)
        row = self._new_revision(
            session,
            public_values=public_values,
            encrypted_secret_values=encrypted_secrets,
            encryption_key_id=encryption_key_id,
            user_id=user_id,
        )
        return SystemRuntimeSettings(
            settings=effective,
            source="database",
            configuration_id=row.configuration_id,
            version=row.version,
            config_sha256=row.config_sha256,
            overridden_public_fields=frozenset(public_values),
            overridden_secret_fields=frozenset(secret_values),
        )

    def restore_field(
        self, session: Session, *, field: str, user_id: str | None
    ) -> SystemRuntimeSettings:
        if field not in PUBLIC_SETTING_FIELDS | SECRET_SETTING_FIELDS:
            raise SystemSettingsError("setting is not managed by the system settings center")
        current = self.resolve(session)
        current_row = (
            session.get(SystemConfigurationVersion, current.configuration_id)
            if current.configuration_id
            else None
        )
        public_values = dict(current_row.public_values or {}) if current_row else {}
        encrypted_secrets = current_row.encrypted_secret_values if current_row else None
        encryption_key_id = current_row.encryption_key_id if current_row else None
        secret_values = (
            self._decrypt_values(current_row)
            if current_row and current_row.encrypted_secret_values
            else {}
        )
        public_values.pop(field, None)
        if field in secret_values:
            secret_values.pop(field)
            encrypted_secrets, encryption_key_id = self._encrypt_values(secret_values)
        effective = self._validated_settings({**public_values, **secret_values})
        self._validate_transition(session, current, effective)
        row = self._new_revision(
            session,
            public_values=public_values,
            encrypted_secret_values=encrypted_secrets,
            encryption_key_id=encryption_key_id,
            user_id=user_id,
        )
        return SystemRuntimeSettings(
            settings=effective,
            source="database",
            configuration_id=row.configuration_id,
            version=row.version,
            config_sha256=row.config_sha256,
            overridden_public_fields=frozenset(public_values),
            overridden_secret_fields=frozenset(secret_values),
        )

    def restore_all(self, session: Session, *, user_id: str | None) -> SystemRuntimeSettings:
        current = self.resolve(session)
        self._validate_transition(session, current, self.settings)
        row = self._new_revision(
            session,
            public_values={},
            encrypted_secret_values=None,
            encryption_key_id=None,
            user_id=user_id,
        )
        return SystemRuntimeSettings(
            settings=self.settings,
            source="database",
            configuration_id=row.configuration_id,
            version=row.version,
            config_sha256=row.config_sha256,
            overridden_public_fields=frozenset(),
            overridden_secret_fields=frozenset(),
        )

    def public_view(self, session: Session) -> dict[str, object]:
        runtime = self.resolve(session)
        values = {
            field: getattr(runtime.settings, field) for field in sorted(PUBLIC_SETTING_FIELDS)
        }
        sources = {
            field: "database" if field in runtime.overridden_public_fields else "environment"
            for field in values
        }
        secret_configured = {
            field: bool(getattr(runtime.settings, field)) for field in sorted(SECRET_SETTING_FIELDS)
        }
        secret_sources = {
            field: "database" if field in runtime.overridden_secret_fields else "environment"
            for field in secret_configured
        }
        return {
            "configuration_id": runtime.configuration_id,
            "version": runtime.version,
            "config_sha256": runtime.config_sha256,
            "source": runtime.source,
            "values": values,
            "sources": sources,
            "secret_configured": secret_configured,
            "secret_sources": secret_sources,
            "read_only_environment": {
                "app_env": runtime.settings.app_env,
                "database_url_configured": bool(runtime.settings.database_url),
                "redis_url_configured": bool(runtime.settings.redis_url),
                "model_gateway_max_concurrency": runtime.settings.model_gateway_max_concurrency,
                "model_settings_encryption_configured": bool(
                    runtime.settings.model_settings_encryption_keys
                ),
            },
            "topology_sha256": runtime.topology_sha256,
        }

    def _environment_runtime(self) -> SystemRuntimeSettings:
        return SystemRuntimeSettings(
            settings=self.settings,
            source="environment",
            configuration_id=None,
            version=0,
            config_sha256=stable_hash(
                {
                    "public_values": {
                        field: getattr(self.settings, field)
                        for field in sorted(PUBLIC_SETTING_FIELDS)
                    },
                    "topology": {
                        "research_execution_mode": self.settings.research_execution_mode,
                        "llm_agent_max_concurrency": self.settings.llm_agent_max_concurrency,
                    },
                }
            ),
            overridden_public_fields=frozenset(),
            overridden_secret_fields=frozenset(),
        )

    def _validated_settings(self, overrides: dict[str, Any]) -> Settings:
        baseline = self.settings.model_dump(mode="python")
        try:
            return Settings.model_validate({**baseline, **overrides})
        except ValueError as exc:
            raise SystemSettingsError(str(exc)) from exc

    @staticmethod
    def _ensure_allowed(
        values: Mapping[str, object], allowed: frozenset[str], *, public: bool = True
    ) -> None:
        invalid = sorted(set(values) - allowed)
        if invalid:
            kind = "public" if public else "secret"
            raise SystemSettingsError(f"unsupported {kind} setting: {', '.join(invalid)}")

    def _new_revision(
        self,
        session: Session,
        *,
        public_values: dict[str, Any],
        encrypted_secret_values: str | None,
        encryption_key_id: str | None,
        user_id: str | None,
    ) -> SystemConfigurationVersion:
        version = int(session.scalar(select(func.max(SystemConfigurationVersion.version))) or 0) + 1
        # Version belongs in the digest so every intentional save is a unique,
        # auditable immutable revision even if the effective values are equal.
        config_sha256 = stable_hash(
            {
                "version": version,
                "public_values": public_values,
                "encrypted_secret_values": encrypted_secret_values,
                "encryption_key_id": encryption_key_id,
            }
        )
        now = datetime.now(UTC)
        row = SystemConfigurationVersion(
            version=version,
            public_values=public_values,
            encrypted_secret_values=encrypted_secret_values,
            encryption_key_id=encryption_key_id,
            config_sha256=config_sha256,
            created_by=user_id,
            created_at=now,
        )
        session.add(row)
        session.flush()
        pointer = session.get(ActiveSystemConfiguration, self.scope)
        if pointer is None:
            session.add(
                ActiveSystemConfiguration(
                    scope=self.scope,
                    configuration_id=row.configuration_id,
                    activated_by=user_id,
                    activated_at=now,
                )
            )
        else:
            pointer.configuration_id = row.configuration_id
            pointer.activated_by = user_id
            pointer.activated_at = now
        session.flush()
        return row

    def _validate_transition(
        self, session: Session, current: SystemRuntimeSettings, target: Settings
    ) -> None:
        if target.research_execution_mode == "DUAL":
            available = _memory_available_bytes()
            if available < 4 * 1024**3:
                raise SystemSettingsError(
                    "DUAL mode requires at least 4 GB available memory; "
                    "free memory or keep SERIAL mode"
                )
            required_gateway_capacity = 2 * target.llm_agent_max_concurrency
            if target.model_gateway_max_concurrency < required_gateway_capacity:
                raise SystemSettingsError(
                    "DUAL mode requires model gateway capacity of at least "
                    f"{required_gateway_capacity}; raise MODEL_GATEWAY_MAX_CONCURRENCY "
                    "or lower per-run concurrency"
                )
        if current.execution_mode != target.research_execution_mode:
            active_research = int(
                session.scalar(
                    select(func.count()).select_from(JobRun).where(
                        JobRun.run_type == "DAILY", JobRun.status.in_(_ACTIVE_RESEARCH_STATUSES)
                    )
                )
                or 0
            )
            active_backtests = int(
                session.scalar(
                    select(func.count()).select_from(BacktestRun).where(
                        BacktestRun.status.in_(_ACTIVE_BACKTEST_STATUSES)
                    )
                )
                or 0
            )
            if active_research or active_backtests:
                raise SystemSettingsError(
                    "cannot change research execution mode while "
                    f"{active_research} research run(s) or {active_backtests} backtest(s) "
                    "are active; "
                    "wait for completion or cancel them first"
                )

    def _cipher(self) -> tuple[MultiFernet, str]:
        raw = self.settings.model_settings_encryption_keys or ""
        keys = [item.strip().encode() for item in raw.split(",") if item.strip()]
        if not keys:
            raise SystemSettingsError(
                "MODEL_SETTINGS_ENCRYPTION_KEYS is required to save sensitive settings"
            )
        try:
            fernets = [Fernet(key) for key in keys]
        except (TypeError, ValueError) as exc:
            raise SystemSettingsError(
                "MODEL_SETTINGS_ENCRYPTION_KEYS contains an invalid key"
            ) from exc
        return MultiFernet(fernets), hashlib.sha256(keys[0]).hexdigest()[:16]

    def _encrypt_values(self, values: dict[str, str]) -> tuple[str | None, str | None]:
        if not values:
            return None, None
        cipher, key_id = self._cipher()
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return cipher.encrypt(encoded).decode("ascii"), key_id

    def _decrypt_values(self, row: SystemConfigurationVersion | None) -> dict[str, str]:
        if row is None or not row.encrypted_secret_values:
            return {}
        try:
            decoded = self._cipher()[0].decrypt(row.encrypted_secret_values.encode("ascii"))
            values = json.loads(decoded.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise SystemSettingsError(
                "system setting secrets cannot be decrypted with configured keys"
            ) from exc
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise SystemSettingsError("encrypted system settings have an invalid shape")
        self._ensure_allowed(values, SECRET_SETTING_FIELDS, public=False)
        return values


def get_effective_settings() -> Settings:
    """Resolve hot-loadable database overrides, falling back safely to .env.

    This is deliberately separate from :func:`get_settings`: bootstrap,
    Alembic and auth must keep using deployment-only settings and must work
    before the system-settings tables exist.
    """

    baseline = get_settings()
    try:
        from ashare_ai.storage.database import SessionLocal

        with SessionLocal() as session:
            return SystemConfigurationService(baseline).resolve(session).settings
    except SQLAlchemyError:
        return baseline
