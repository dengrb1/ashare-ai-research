from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ashare_ai.agents.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleStructuredLLMClient,
)
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.storage.models import ActiveModelConfiguration, ModelConfigurationVersion

Purpose = Literal["search", "research"]


class ModelSettingsError(RuntimeError):
    pass


class ModelConnectionProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


@dataclass(frozen=True)
class ModelSettingsDraft:
    base_url: str
    api_key: str | None
    search_model: str = "gpt-5.6-luna"
    search_reasoning_effort: str = "low"
    research_model: str = "gpt-5.6-sol"
    research_reasoning_effort: str = "high"
    timeout_seconds: float = 90.0
    enabled: bool = True


@dataclass(frozen=True)
class ModelRuntimeConfiguration:
    source: Literal["database", "environment"]
    configuration_id: str | None
    version: int
    config_sha256: str
    provider: str
    base_url: str
    api_key: str
    search_model: str
    search_reasoning_effort: str
    research_model: str
    research_reasoning_effort: str
    timeout_seconds: float
    enabled: bool

    def model_for(self, purpose: Purpose) -> tuple[str, str]:
        if purpose == "search":
            return self.search_model, self.search_reasoning_effort
        return self.research_model, self.research_reasoning_effort

    def manifest_reference(self) -> dict[str, object]:
        return {
            "source": self.source,
            "configuration_id": self.configuration_id,
            "version": self.version,
            "config_sha256": self.config_sha256,
            "provider": self.provider,
            "search_model": self.search_model,
            "search_reasoning_effort": self.search_reasoning_effort,
            "research_model": self.research_model,
            "research_reasoning_effort": self.research_reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class ModelProbeResult:
    reachable: bool
    message: str
    model: str
    checked_at: datetime
    structured_output_supported: bool = True
    streaming_supported: bool = False


class ModelConfigurationService:
    scope = "default"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def resolve(
        self, session: Session, *, require_enabled: bool = True
    ) -> ModelRuntimeConfiguration | None:
        pointer = session.get(ActiveModelConfiguration, self.scope)
        if pointer is not None:
            row = session.get(ModelConfigurationVersion, pointer.configuration_id)
            if row is None:
                raise ModelSettingsError("active model configuration is missing")
            runtime = self._runtime_from_row(row)
            return runtime if runtime.enabled or not require_enabled else None
        environment_runtime = self._environment_runtime()
        return (
            environment_runtime
            if environment_runtime is None
            or environment_runtime.enabled
            or not require_enabled
            else None
        )

    def resolve_pinned(
        self, session: Session, reference: dict[str, object]
    ) -> ModelRuntimeConfiguration | None:
        if not reference.get("enabled", True):
            return None
        configuration_id = reference.get("configuration_id")
        if isinstance(configuration_id, str) and configuration_id:
            row = session.get(ModelConfigurationVersion, configuration_id)
            if row is None:
                raise ModelSettingsError("pinned model configuration no longer exists")
            runtime = self._runtime_from_row(row)
        else:
            environment_runtime = self._environment_runtime()
            if environment_runtime is None:
                raise ModelSettingsError("pinned environment model configuration is unavailable")
            runtime = environment_runtime
        if runtime.config_sha256 != reference.get("config_sha256"):
            raise ModelSettingsError("pinned model configuration hash mismatch")
        return runtime

    def bootstrap_from_environment(self, session: Session) -> None:
        if session.get(ActiveModelConfiguration, self.scope) is not None:
            return
        runtime = self._environment_runtime()
        if runtime is None or not self.settings.model_settings_encryption_keys:
            return
        self.save_and_activate(
            session,
            ModelSettingsDraft(
                base_url=runtime.base_url,
                api_key=runtime.api_key,
                search_model="gpt-5.6-luna",
                search_reasoning_effort="low",
                research_model=runtime.research_model,
                research_reasoning_effort=runtime.research_reasoning_effort,
                timeout_seconds=runtime.timeout_seconds,
                enabled=True,
            ),
            user_id=None,
            probe=None,
        )

    def save_and_activate(
        self,
        session: Session,
        draft: ModelSettingsDraft,
        *,
        user_id: str | None,
        probe: ModelProbeResult | None,
    ) -> ModelConfigurationVersion:
        base_url = normalize_base_url(draft.base_url, self.settings)
        current = self.resolve(session, require_enabled=False)
        api_key = draft.api_key or (current.api_key if current else None)
        if not api_key:
            raise ModelSettingsError("API key is required")
        _validate_effort(draft.search_reasoning_effort)
        _validate_effort(draft.research_reasoning_effort)
        if not draft.search_model.strip() or not draft.research_model.strip():
            raise ModelSettingsError("search and research models are required")
        if not 1 <= draft.timeout_seconds <= 600:
            raise ModelSettingsError("timeout_seconds must be between 1 and 600")
        config_hash = _config_hash(
            base_url=base_url,
            api_key=api_key,
            search_model=draft.search_model.strip(),
            search_reasoning_effort=draft.search_reasoning_effort,
            research_model=draft.research_model.strip(),
            research_reasoning_effort=draft.research_reasoning_effort,
            timeout_seconds=draft.timeout_seconds,
            enabled=draft.enabled,
        )
        row = session.scalar(
            select(ModelConfigurationVersion).where(
                ModelConfigurationVersion.config_sha256 == config_hash
            )
        )
        now = datetime.now(UTC)
        if row is None:
            fernet, key_id = self._cipher()
            version = int(
                session.scalar(select(func.max(ModelConfigurationVersion.version))) or 0
            ) + 1
            row = ModelConfigurationVersion(
                version=version,
                provider="openai-compatible",
                base_url=base_url,
                encrypted_api_key=fernet.encrypt(api_key.encode()).decode(),
                encryption_key_id=key_id,
                search_model=draft.search_model.strip(),
                search_reasoning_effort=draft.search_reasoning_effort,
                research_model=draft.research_model.strip(),
                research_reasoning_effort=draft.research_reasoning_effort,
                timeout_seconds=draft.timeout_seconds,
                enabled=draft.enabled,
                config_sha256=config_hash,
                created_by=user_id,
                created_at=now,
            )
            session.add(row)
            session.flush()
        pointer = session.get(ActiveModelConfiguration, self.scope)
        if pointer is None:
            pointer = ActiveModelConfiguration(
                scope=self.scope,
                configuration_id=row.configuration_id,
                activated_by=user_id,
                activated_at=now,
                last_check_status="UNTESTED",
            )
            session.add(pointer)
        else:
            pointer.configuration_id = row.configuration_id
            pointer.activated_by = user_id
            pointer.activated_at = now
        if probe is not None:
            pointer.last_checked_at = probe.checked_at
            pointer.last_check_status = "REACHABLE" if probe.reachable else "FAILED"
            pointer.last_check_message = probe.message
            pointer.structured_output_supported = probe.structured_output_supported
            pointer.streaming_supported = probe.streaming_supported
        session.flush()
        return row

    async def probe(self, draft: ModelSettingsDraft, session: Session) -> ModelProbeResult:
        base_url = normalize_base_url(draft.base_url, self.settings)
        current = self.resolve(session, require_enabled=False)
        api_key = draft.api_key or (current.api_key if current else None)
        if not api_key:
            raise ModelSettingsError("API key is required")
        model = draft.search_model.strip()
        client = OpenAICompatibleStructuredLLMClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            reasoning_effort=draft.search_reasoning_effort,
            timeout_seconds=draft.timeout_seconds,
            max_retries=0,
        )
        checked_at = datetime.now(UTC)
        try:
            result = await client.generate_structured(
                schema=ModelConnectionProbe,
                messages=(
                    {
                        "role": "system",
                        "content": "Return only the requested schema. Set ok to true.",
                    },
                    {"role": "user", "content": "Connectivity probe."},
                ),
                idempotency_key=stable_hash(
                    {"kind": "model-settings-probe", "model": model, "at": checked_at}
                ),
            )
            if result.output.get("ok") is not True:
                raise ModelSettingsError("model probe returned an unexpected result")
        except (OpenAICompatibleError, httpx.HTTPError) as exc:
            raise ModelSettingsError(_safe_error(exc)) from exc
        streaming_supported = False
        try:
            streaming_supported = await client.probe_stream()
        except (OpenAICompatibleError, httpx.HTTPError):
            # A compatible one-shot Responses gateway remains usable for chat;
            # the persisted capability makes the eventual degradation visible.
            streaming_supported = False
        return ModelProbeResult(
            reachable=True,
            message=(
                "Responses API structured and streaming probes succeeded"
                if streaming_supported
                else "Responses API structured probe succeeded; streaming will degrade"
            ),
            model=model,
            checked_at=checked_at,
            structured_output_supported=True,
            streaming_supported=streaming_supported,
        )

    async def list_models(self, draft: ModelSettingsDraft, session: Session) -> list[str]:
        base_url = normalize_base_url(draft.base_url, self.settings)
        current = self.resolve(session, require_enabled=False)
        api_key = draft.api_key or (current.api_key if current else None)
        if not api_key:
            raise ModelSettingsError("API key is required")
        try:
            async with httpx.AsyncClient(timeout=draft.timeout_seconds) as client:
                response = await client.get(
                    f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelSettingsError(_safe_error(exc)) from exc
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return sorted(
            {
                str(item["id"])
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )

    def status(self, session: Session) -> dict[str, object]:
        runtime = self.resolve(session, require_enabled=False)
        pointer = session.get(ActiveModelConfiguration, self.scope)
        configured = runtime is not None
        reachable = bool(pointer and pointer.last_check_status == "REACHABLE")
        message = (
            pointer.last_check_message
            if pointer and pointer.last_check_message
            else "已配置但尚未完成连通性测试"
            if configured
            else "尚未配置模型 API"
        )
        return {
            "configured": configured,
            "reachable": reachable,
            "degraded": configured and not reachable,
            "enabled": bool(runtime and runtime.enabled),
            "message": message,
            "checked_at": pointer.last_checked_at if pointer else None,
            "structured_output_supported": bool(
                pointer and pointer.structured_output_supported
            ),
            "streaming_supported": bool(pointer and pointer.streaming_supported),
        }

    def _runtime_from_row(self, row: ModelConfigurationVersion) -> ModelRuntimeConfiguration:
        try:
            api_key = self._cipher()[0].decrypt(row.encrypted_api_key.encode()).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ModelSettingsError(
                "model API key cannot be decrypted with configured keys"
            ) from exc
        return ModelRuntimeConfiguration(
            source="database",
            configuration_id=row.configuration_id,
            version=row.version,
            config_sha256=row.config_sha256,
            provider=row.provider,
            base_url=row.base_url,
            api_key=api_key,
            search_model=row.search_model,
            search_reasoning_effort=row.search_reasoning_effort,
            research_model=row.research_model,
            research_reasoning_effort=row.research_reasoning_effort,
            timeout_seconds=row.timeout_seconds,
            enabled=row.enabled,
        )

    def _environment_runtime(self) -> ModelRuntimeConfiguration | None:
        if (
            self.settings.agent_backend != "openai_compatible"
            or not self.settings.llm_base_url
            or not self.settings.llm_api_key
        ):
            return None
        base_url = normalize_base_url(self.settings.llm_base_url, self.settings)
        config_hash = _config_hash(
            base_url=base_url,
            api_key=self.settings.llm_api_key,
            search_model="gpt-5.6-luna",
            search_reasoning_effort="low",
            research_model=self.settings.llm_model,
            research_reasoning_effort=self.settings.llm_reasoning_effort,
            timeout_seconds=self.settings.llm_timeout_seconds,
            enabled=True,
        )
        return ModelRuntimeConfiguration(
            source="environment",
            configuration_id=None,
            version=0,
            config_sha256=config_hash,
            provider="openai-compatible",
            base_url=base_url,
            api_key=self.settings.llm_api_key,
            search_model="gpt-5.6-luna",
            search_reasoning_effort="low",
            research_model=self.settings.llm_model,
            research_reasoning_effort=self.settings.llm_reasoning_effort,
            timeout_seconds=self.settings.llm_timeout_seconds,
            enabled=True,
        )

    def _cipher(self) -> tuple[MultiFernet, str]:
        raw = self.settings.model_settings_encryption_keys or ""
        keys = [item.strip().encode() for item in raw.split(",") if item.strip()]
        if not keys:
            raise ModelSettingsError("MODEL_SETTINGS_ENCRYPTION_KEYS is required")
        try:
            fernets = [Fernet(key) for key in keys]
        except (TypeError, ValueError) as exc:
            raise ModelSettingsError(
                "MODEL_SETTINGS_ENCRYPTION_KEYS contains an invalid key"
            ) from exc
        key_id = hashlib.sha256(keys[0]).hexdigest()[:16]
        return MultiFernet(fernets), key_id


def normalize_base_url(value: str, settings: Settings | None = None) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or "," in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelSettingsError("Base URL 必须是合法的 http(s) 地址")
    if settings is not None and settings.app_env.casefold() == "production":
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme != "https":
            raise ModelSettingsError("生产环境模型 Base URL 必须使用 HTTPS")
        if host not in settings.model_allowed_host_set:
            raise ModelSettingsError("模型 Base URL 主机不在 MODEL_ALLOWED_HOSTS 白名单中")
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def _config_hash(**values: object) -> str:
    api_key = str(values.pop("api_key"))
    return stable_hash({**values, "api_key_sha256": hashlib.sha256(api_key.encode()).hexdigest()})


def _validate_effort(value: str) -> None:
    if value not in {"low", "medium", "high", "xhigh"}:
        raise ModelSettingsError("reasoning effort must be low, medium, high or xhigh")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"model endpoint returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return "model endpoint connection failed"
    if isinstance(exc, OpenAICompatibleError):
        return "model endpoint rejected the structured-output probe"
    return "model endpoint validation failed"
