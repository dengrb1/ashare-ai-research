from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
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
from ashare_ai.agents.provider_failover import ProviderClient, ProviderFailoverStructuredLLMClient
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.storage.models import (
    ActiveModelConfiguration,
    ModelConfigurationVersion,
    ModelProbeLog,
)

CachePolicy = Literal["GROK", "OPENAI", "COMPATIBLE"]


@dataclass(frozen=True)
class ModelProbeDiagnostic:
    model: str
    purpose: str
    protocol: str
    endpoint_path: str
    request_mode: str
    outcome: Literal["SUCCEEDED", "FAILED"]
    http_status: int | None
    error_code: str | None
    message: str
    duration_ms: int
    header_presence: dict[str, bool]
    created_at: datetime


class ModelSettingsError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MODEL_SETTINGS_ERROR",
        diagnostics: tuple[ModelProbeDiagnostic, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics


class ModelConnectionProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


@dataclass(frozen=True)
class ModelProfileDraft:
    model: str
    cache_policy: CachePolicy = "COMPATIBLE"
    context_window_tokens: int = 128000
    output_token_reserve: int = 8192
    reasoning_token_reserve: int = 0
    input_price_per_million: Decimal = Decimal("0")
    cached_input_price_per_million: Decimal = Decimal("0")
    cache_write_price_per_million: Decimal = Decimal("0")
    output_price_per_million: Decimal = Decimal("0")


@dataclass(frozen=True)
class ModelRuntimeProfile(ModelProfileDraft):
    @property
    def input_budget_tokens(self) -> int:
        return self.context_window_tokens - self.output_token_reserve - self.reasoning_token_reserve

    def public_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "cache_policy": self.cache_policy,
            "context_window_tokens": self.context_window_tokens,
            "output_token_reserve": self.output_token_reserve,
            "reasoning_token_reserve": self.reasoning_token_reserve,
            "input_price_per_million": str(self.input_price_per_million),
            "cached_input_price_per_million": str(self.cached_input_price_per_million),
            "cache_write_price_per_million": str(self.cache_write_price_per_million),
            "output_price_per_million": str(self.output_price_per_million),
        }


@dataclass(frozen=True)
class ModelSettingsDraft:
    base_url: str
    api_key: str | None
    research_model: str = "gpt-5.6-sol"
    research_reasoning_effort: str = "high"
    model_profiles: tuple[ModelProfileDraft, ...] = ()
    timeout_seconds: float = 90.0
    enabled: bool = True
    providers: tuple[dict[str, Any], ...] | None = None
    primary_provider_id: str | None = None
    fallback_provider_ids: tuple[str, ...] = ()
    routing: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRuntimeConfiguration:
    source: Literal["database", "environment"]
    configuration_id: str | None
    version: int
    config_sha256: str
    provider: str
    base_url: str
    api_key: str
    research_model: str
    research_reasoning_effort: str
    model_profiles: tuple[ModelRuntimeProfile, ...]
    timeout_seconds: float
    enabled: bool
    providers: tuple[dict[str, Any], ...] = ()
    primary_provider_id: str | None = None
    fallback_provider_ids: tuple[str, ...] = ()
    routing: dict[str, str] = field(default_factory=dict)
    encrypted_provider_keys: dict[str, str] = field(default_factory=dict)

    def model_for(self, purpose: str = "research") -> tuple[str, str]:
        key = _purpose_alias(purpose)
        return self.routing.get(key, self.research_model), self.research_reasoning_effort

    def profile_for(self, model: str) -> ModelRuntimeProfile:
        for profile in self.model_profiles:
            if profile.model == model:
                return profile
        return _default_profile(model)

    def manifest_reference(self) -> dict[str, object]:
        return {
            "source": self.source,
            "configuration_id": self.configuration_id,
            "version": self.version,
            "config_sha256": self.config_sha256,
            "provider": self.provider,
            "research_model": self.research_model,
            "research_reasoning_effort": self.research_reasoning_effort,
            "model_profiles": [profile.public_dict() for profile in self.model_profiles],
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
            "providers": list(self.providers),
            "primary_provider_id": self.primary_provider_id,
            "fallback_provider_ids": list(self.fallback_provider_ids),
            "routing": dict(self.routing),
        }


@dataclass(frozen=True)
class ModelProbeResult:
    reachable: bool
    message: str
    model: str
    checked_at: datetime
    structured_output_supported: bool = True
    streaming_supported: bool = False
    streaming_checked: bool = False
    diagnostics: tuple[ModelProbeDiagnostic, ...] = ()


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
            if environment_runtime is None or environment_runtime.enabled or not require_enabled
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
                research_model=runtime.research_model,
                research_reasoning_effort=runtime.research_reasoning_effort,
                model_profiles=runtime.model_profiles,
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
        _validate_effort(draft.research_reasoning_effort)
        if not draft.research_model.strip():
            raise ModelSettingsError("research model is required")
        if not 1 <= draft.timeout_seconds <= 600:
            raise ModelSettingsError("timeout_seconds must be between 1 and 600")
        profiles = _normalize_profiles(
            draft.model_profiles,
            models=(draft.research_model.strip(),),
        )
        provider_items = tuple(draft.providers) if draft.providers is not None else (current.providers if current else ())
        for provider in provider_items:
            if not isinstance(provider, dict):
                raise ModelSettingsError("供应商配置必须是对象")
            _validate_provider_metadata(provider)
        primary_provider_id = draft.primary_provider_id if draft.providers is not None else (draft.primary_provider_id or (current.primary_provider_id if current else None))
        fallback_provider_ids = tuple(draft.fallback_provider_ids) if draft.providers is not None else (tuple(draft.fallback_provider_ids) or (current.fallback_provider_ids if current else ()))
        routing = dict(draft.routing) if draft.providers is not None else (dict(draft.routing) or (dict(current.routing) if current else {}))
        provider_ids = [str(item.get("provider_id") or item.get("id") or "").strip() for item in provider_items]
        if any(not provider_id for provider_id in provider_ids) or len(set(provider_ids)) != len(provider_ids):
            raise ModelSettingsError("供应商 ID 必须存在且唯一")
        if primary_provider_id and primary_provider_id not in provider_ids:
            raise ModelSettingsError("主供应商必须属于当前供应商列表")
        if any(provider_id not in provider_ids for provider_id in fallback_provider_ids):
            raise ModelSettingsError("备用供应商必须属于当前供应商列表")
        public_provider_hash = [
            {
                key: value
                for key, value in _public_provider_dict(dict(item)).items()
            }
            | {
                "api_key_configured": bool(item.get("api_key") or item.get("api_key_configured")),
                "api_key_sha256": hashlib.sha256(str(item.get("api_key")).encode()).hexdigest()
                if item.get("api_key") else None,
            }
            for item in provider_items
        ]
        config_hash = _config_hash(
            base_url=base_url,
            api_key=api_key,
            research_model=draft.research_model.strip(),
            research_reasoning_effort=draft.research_reasoning_effort,
            model_profiles=[profile.public_dict() for profile in profiles],
            timeout_seconds=draft.timeout_seconds,
            enabled=draft.enabled,
            providers=public_provider_hash,
            primary_provider_id=primary_provider_id,
            fallback_provider_ids=list(fallback_provider_ids),
            routing=routing,
        )
        row = session.scalar(
            select(ModelConfigurationVersion).where(
                ModelConfigurationVersion.config_sha256 == config_hash
            )
        )
        now = datetime.now(UTC)
        if row is None:
            fernet, key_id = self._cipher()
            provider_keys: dict[str, str] = {}
            public_providers: list[dict[str, Any]] = []
            previous_provider_keys = dict(current.encrypted_provider_keys) if current else {}
            for item in provider_items:
                provider = _public_provider_dict(dict(item))
                provider_id = str(provider.get("provider_id") or provider.get("id") or "").strip()
                if not provider_id:
                    raise ModelSettingsError("provider_id is required")
                secret = item.get("api_key")
                if secret:
                    provider_keys[provider_id] = fernet.encrypt(str(secret).encode()).decode()
                elif provider_id in previous_provider_keys:
                    # Keep an existing encrypted secret when the settings form
                    # intentionally leaves the password field blank.
                    provider_keys[provider_id] = previous_provider_keys[provider_id]
                provider["provider_id"] = provider_id
                provider["api_key_configured"] = bool(secret or provider.get("api_key_configured") or provider_id in provider_keys)
                public_providers.append(provider)
            version = (
                int(session.scalar(select(func.max(ModelConfigurationVersion.version))) or 0) + 1
            )
            row = ModelConfigurationVersion(
                version=version,
                provider="openai-compatible",
                base_url=base_url,
                encrypted_api_key=fernet.encrypt(api_key.encode()).decode(),
                encryption_key_id=key_id,
                research_model=draft.research_model.strip(),
                research_reasoning_effort=draft.research_reasoning_effort,
                model_profiles=[_profile_storage_dict(profile) for profile in profiles],
                timeout_seconds=draft.timeout_seconds,
                enabled=draft.enabled,
                providers=public_providers,
                encrypted_provider_keys=provider_keys,
                primary_provider_id=primary_provider_id,
                fallback_provider_ids=list(fallback_provider_ids),
                routing=routing,
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
            if probe.streaming_checked:
                pointer.streaming_supported = probe.streaming_supported
        session.flush()
        return row

    async def probe(
        self,
        draft: ModelSettingsDraft,
        session: Session,
        *,
        include_streaming: bool = False,
    ) -> ModelProbeResult:
        """Probe every configured model; stream probing is an explicit opt-in."""

        base_url = normalize_base_url(draft.base_url, self.settings)
        current = self.resolve(session, require_enabled=False)
        api_key = draft.api_key or (current.api_key if current else None)
        if not api_key:
            raise ModelSettingsError("API key is required")
        profiles = _normalize_profiles(
            draft.model_profiles,
            models=(draft.research_model.strip(),),
        )
        profile_by_model = {item.model: item for item in profiles}
        configured_models = ((draft.research_model.strip(), draft.research_reasoning_effort),)
        provider_specs: list[tuple[str, str, str, str, str]] = []
        if draft.providers:
            previous = self.resolve(session, require_enabled=False)
            previous_keys = dict(previous.encrypted_provider_keys) if previous else {}
            cipher = self._cipher()[0]
            for item in draft.providers:
                if not item.get("enabled", True):
                    continue
                provider_id = str(item.get("provider_id") or item.get("id") or "").strip()
                if not provider_id:
                    raise ModelSettingsError("provider_id is required")
                secret = str(item.get("api_key") or "")
                if not secret and provider_id in previous_keys:
                    try:
                        secret = cipher.decrypt(previous_keys[provider_id].encode()).decode()
                    except (InvalidToken, UnicodeDecodeError) as exc:
                        raise ModelSettingsError(f"供应商 {provider_id} 的 API Key 无法解密") from exc
                if not secret:
                    secret = api_key
                provider_specs.append((
                    provider_id,
                    normalize_base_url(str(item.get("base_url") or item.get("endpoint") or base_url), self.settings),
                    secret,
                    str(item.get("model") or item.get("research_model") or draft.research_model.strip()),
                    str(item.get("reasoning_effort") or draft.research_reasoning_effort),
                ))
            configured_models = tuple((spec[3], spec[4]) for spec in provider_specs)
        purposes: dict[str, list[str]] = {}
        for model, _effort in configured_models:
            purposes.setdefault(model, []).append("research")
        clients: list[OpenAICompatibleStructuredLLMClient] = []
        diagnostics: list[ModelProbeDiagnostic] = []
        failures: list[tuple[str, str, str]] = []
        checked_at = datetime.now(UTC)
        async def close_probe_client(value: object) -> None:
            close = getattr(value, "aclose", None)
            if close is not None:
                await close()
        probe_targets = provider_specs or [("default", base_url, api_key, draft.research_model.strip(), draft.research_reasoning_effort)]
        for provider_id, provider_base_url, provider_api_key, model, effort in probe_targets:
            started = asyncio.get_running_loop().time()
            client = OpenAICompatibleStructuredLLMClient(
                base_url=provider_base_url,
                api_key=provider_api_key,
                model=model,
                reasoning_effort=effort,
                timeout_seconds=min(draft.timeout_seconds, 8.0),
                max_retries=0,
                cache_policy=profile_by_model.get(model, _default_profile(model)).cache_policy,
            )
            try:
                result = await asyncio.wait_for(
                    client.generate_structured(
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
                    ),
                    timeout=8.0,
                )
                if result.output.get("ok") is not True:
                    raise ModelSettingsError(
                        "model probe returned an unexpected result",
                        code="MODEL_INVALID_STRUCTURED_OUTPUT",
                    )
            except TimeoutError as exc:
                diagnostics.append(
                    _probe_diagnostic(
                        client=client,
                        model=model,
                        purpose=f"{provider_id}:" + "/".join(purposes.get(model, ("unknown",))),
                        outcome="FAILED",
                        error_code="MODEL_PROBE_TIMEOUT",
                        message=f"模型 {model} 探测超时",
                        started=started,
                        checked_at=checked_at,
                    )
                )
                failures.append((provider_id, "MODEL_PROBE_TIMEOUT", f"模型 {model} 探测超时"))
                await close_probe_client(client)
                continue
            except ModelSettingsError as exc:
                diagnostics.append(
                    _probe_diagnostic(
                        client=client,
                        model=model,
                        purpose=f"{provider_id}:" + "/".join(purposes.get(model, ("unknown",))),
                        outcome="FAILED",
                        error_code=exc.code,
                        message=str(exc),
                        started=started,
                        checked_at=checked_at,
                    )
                )
                failures.append((provider_id, exc.code, str(exc)))
                await close_probe_client(client)
                continue
            except (OpenAICompatibleError, httpx.HTTPError) as exc:
                error_code = _safe_error_code(exc)
                diagnostics.append(
                    _probe_diagnostic(
                        client=client,
                        model=model,
                        purpose=f"{provider_id}:" + "/".join(purposes.get(model, ("unknown",))),
                        outcome="FAILED",
                        error_code=error_code,
                        message=_safe_error(exc),
                        started=started,
                        checked_at=checked_at,
                    )
                )
                failures.append((provider_id, error_code, _safe_error(exc)))
                await close_probe_client(client)
                continue
            diagnostics.append(
                _probe_diagnostic(
                    client=client,
                    model=model,
                    purpose=f"{provider_id}:" + "/".join(purposes.get(model, ("unknown",))),
                    outcome="SUCCEEDED",
                    error_code=None,
                    message="结构化探测成功",
                    started=started,
                    checked_at=checked_at,
                )
            )
            clients.append(client)
        if failures:
            provider_id, error_code, message = failures[0]
            detail = "; ".join(
                f"{failed_provider}: {failed_message}"
                for failed_provider, _failed_code, failed_message in failures
            )
            for open_client in clients:
                await close_probe_client(open_client)
            raise ModelSettingsError(
                f"供应商探测失败（{detail}）",
                code=error_code,
                diagnostics=tuple(diagnostics),
            )
        streaming_supported = False
        if include_streaming:
            stream_started = asyncio.get_running_loop().time()
            stream_client = clients[0]
            try:
                streaming_supported = await asyncio.wait_for(
                    stream_client.probe_stream(), timeout=8.0
                )
                diagnostics.append(
                    _probe_diagnostic(
                        client=stream_client,
                        model=draft.research_model.strip(),
                        purpose="research",
                        outcome="SUCCEEDED" if streaming_supported else "FAILED",
                        error_code=None if streaming_supported else "MODEL_STREAMING_UNSUPPORTED",
                        message=(
                            "流式探测成功"
                            if streaming_supported
                            else "流式探测未完成，将使用非流式降级"
                        ),
                        started=stream_started,
                        checked_at=checked_at,
                    )
                )
            except TimeoutError:
                streaming_supported = False
                diagnostics.append(
                    _probe_diagnostic(
                        client=stream_client,
                        model=draft.research_model.strip(),
                        purpose="research",
                        outcome="FAILED",
                        error_code="MODEL_STREAM_PROBE_TIMEOUT",
                        message="流式探测超时，将使用非流式降级",
                        started=stream_started,
                        checked_at=checked_at,
                    )
                )
            except (OpenAICompatibleError, httpx.HTTPError) as exc:
                # A compatible one-shot Responses gateway remains usable for chat;
                # the persisted capability makes the eventual degradation visible.
                streaming_supported = False
                diagnostics.append(
                    _probe_diagnostic(
                        client=stream_client,
                        model=draft.research_model.strip(),
                        purpose="research",
                        outcome="FAILED",
                        error_code=_safe_error_code(exc),
                        message="流式探测失败，将使用非流式降级",
                        started=stream_started,
                        checked_at=checked_at,
                    )
                )
        for open_client in clients:
            await close_probe_client(open_client)
        return ModelProbeResult(
            reachable=True,
            message=(
                "所有已配置模型的 Responses API 结构化探测与搜索模型流式探测均成功"
                if streaming_supported
                else "所有已配置模型的 Responses API 结构化探测成功；搜索模型流式响应将降级"
            ),
            model=draft.research_model.strip(),
            checked_at=checked_at,
            structured_output_supported=True,
            streaming_supported=streaming_supported,
            streaming_checked=include_streaming,
            diagnostics=tuple(diagnostics),
        )

    def persist_probe_logs(
        self, session: Session, diagnostics: tuple[ModelProbeDiagnostic, ...]
    ) -> None:
        for diagnostic in diagnostics:
            session.add(
                ModelProbeLog(
                    model=diagnostic.model,
                    purpose=diagnostic.purpose,
                    protocol=diagnostic.protocol,
                    endpoint_path=diagnostic.endpoint_path,
                    request_mode=diagnostic.request_mode,
                    outcome=diagnostic.outcome,
                    http_status=diagnostic.http_status,
                    error_code=diagnostic.error_code,
                    message=diagnostic.message,
                    duration_ms=diagnostic.duration_ms,
                    header_presence=diagnostic.header_presence,
                    created_at=diagnostic.created_at,
                )
            )
        if diagnostics:
            session.flush()

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
            raise ModelSettingsError(_safe_error(exc), code=_safe_error_code(exc)) from exc
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
            "structured_output_supported": bool(pointer and pointer.structured_output_supported),
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
            research_model=row.research_model,
            research_reasoning_effort=row.research_reasoning_effort,
            model_profiles=_profiles_from_storage(
                row.model_profiles,
                models=(row.research_model,),
            ),
            timeout_seconds=row.timeout_seconds,
            enabled=row.enabled,
            providers=tuple(_public_provider_dict(dict(item)) for item in (row.providers or [])),
            primary_provider_id=row.primary_provider_id or (row.providers[0].get("provider_id") if row.providers else None),
            fallback_provider_ids=tuple(row.fallback_provider_ids or []),
            routing=dict(row.routing or {}),
            encrypted_provider_keys=dict(row.encrypted_provider_keys or {}),
        )

    def client_for(
        self,
        runtime: ModelRuntimeConfiguration,
        *,
        purpose: str = "research",
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_retries: int = 2,
        reuse_client: bool = False,
        timeout_seconds: float | None = None,
    ) -> OpenAICompatibleStructuredLLMClient | ProviderFailoverStructuredLLMClient:
        """Build the ordered provider chain for one runtime request.

        Provider metadata is public and never contains a secret. The encrypted
        provider key map is decrypted only here, at the point where a client is
        constructed, so it cannot leak through API responses or manifests.
        """
        selected_model = model or runtime.routing.get(_purpose_alias(purpose)) or runtime.research_model
        selected_effort = reasoning_effort or runtime.research_reasoning_effort
        request_timeout = timeout_seconds or runtime.timeout_seconds
        providers = [dict(item) for item in runtime.providers if item.get("enabled", True)]
        if not providers:
            return OpenAICompatibleStructuredLLMClient(
                base_url=runtime.base_url,
                api_key=runtime.api_key,
                model=selected_model,
                reasoning_effort=selected_effort,
                timeout_seconds=request_timeout,
                max_retries=max_retries,
                cache_policy=runtime.profile_for(selected_model).cache_policy,
                reuse_client=reuse_client,
            )
        primary = runtime.primary_provider_id
        fallback = list(runtime.fallback_provider_ids)
        by_id = {str(item.get("provider_id") or item.get("id")): item for item in providers}
        ordered_ids: list[str] = []
        for provider_id in ([primary] if primary else []) + fallback:
            if provider_id and provider_id in by_id and provider_id not in ordered_ids:
                ordered_ids.append(provider_id)
        if not ordered_ids:
            for item in sorted(providers, key=lambda value: int(value.get("priority", 0))):
                provider_id = str(item.get("provider_id") or item.get("id") or "")
                if provider_id and provider_id not in ordered_ids:
                    ordered_ids.append(provider_id)
        fernet = self._cipher()[0]
        clients: list[ProviderClient] = []
        for provider_id in ordered_ids:
            item = by_id[provider_id]
            encrypted = runtime.encrypted_provider_keys.get(provider_id)
            secret = runtime.api_key
            if encrypted:
                try:
                    secret = fernet.decrypt(encrypted.encode()).decode()
                except (InvalidToken, UnicodeDecodeError) as exc:
                    raise ModelSettingsError(f"供应商 {provider_id} 的 API Key 无法解密") from exc
            provider_model = str(item.get("model") or item.get("research_model") or selected_model)
            provider_url = str(item.get("base_url") or item.get("endpoint") or runtime.base_url)
            provider_url = normalize_base_url(provider_url, self.settings)
            clients.append(ProviderClient(
                provider_id=provider_id,
                client=OpenAICompatibleStructuredLLMClient(
                    base_url=provider_url,
                    api_key=secret,
                    model=provider_model,
                    reasoning_effort=str(item.get("reasoning_effort") or selected_effort),
                    timeout_seconds=float(item.get("timeout_seconds") or request_timeout),
                    max_retries=max_retries,
                    cache_policy=runtime.profile_for(provider_model).cache_policy,
                    reuse_client=reuse_client,
                ),
            ))
        return ProviderFailoverStructuredLLMClient(clients)

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
            research_model=self.settings.llm_model,
            research_reasoning_effort=self.settings.llm_reasoning_effort,
            model_profiles=[
                _default_profile(model).public_dict()
                for model in (self.settings.llm_model,)
            ],
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
            research_model=self.settings.llm_model,
            research_reasoning_effort=self.settings.llm_reasoning_effort,
            model_profiles=_normalize_profiles(
                (), models=(self.settings.llm_model,)
            ),
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


def _probe_diagnostic(
    *,
    client: OpenAICompatibleStructuredLLMClient,
    model: str,
    purpose: str,
    outcome: Literal["SUCCEEDED", "FAILED"],
    error_code: str | None,
    message: str,
    started: float,
    checked_at: datetime,
) -> ModelProbeDiagnostic:
    elapsed_ms = max(0, int((asyncio.get_running_loop().time() - started) * 1000))
    return ModelProbeDiagnostic(
        model=model,
        purpose=purpose,
        protocol=str(getattr(client, "last_protocol", "RESPONSES")),
        endpoint_path=str(getattr(client, "last_endpoint_path", "/responses")),
        request_mode=str(getattr(client, "last_request_mode", "json_schema")),
        outcome=outcome,
        http_status=getattr(client, "last_status_code", None),
        error_code=error_code,
        message=message,
        duration_ms=elapsed_ms,
        header_presence={
            "Authorization": True,
            "Idempotency-Key": True,
            "Content-Type": True,
        },
        created_at=checked_at,
    )


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


def _is_secret_field(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return (
        normalized in {
            "api_key",
            "encrypted_api_key",
            "password",
            "secret",
            "token",
            "credential",
            "private_key",
        }
        or normalized.endswith(("_secret", "_token", "_password", "_credential", "_key"))
    )


def _validate_provider_metadata(value: object, *, _depth: int = 0, _path: str = "provider") -> None:
    """Reject nested credentials before they can reach persistence or logs."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if _is_secret_field(key_text) and (_depth > 0 or key_text.casefold().replace("-", "_") != "api_key"):
                raise ModelSettingsError("供应商配置不得包含嵌套凭据字段")
            _validate_provider_metadata(child, _depth=_depth + 1, _path=f"{_path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_provider_metadata(child, _depth=_depth + 1, _path=f"{_path}[{index}]")


def _public_provider_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove secret fields from legacy provider metadata."""
    def clean(item: object, *, depth: int) -> object:
        if isinstance(item, dict):
            return {
                str(key): clean(child, depth=depth + 1)
                for key, child in item.items()
                if not _is_secret_field(str(key))
            }
        if isinstance(item, list):
            return [clean(child, depth=depth + 1) for child in item]
        if isinstance(item, tuple):
            return [clean(child, depth=depth + 1) for child in item]
        return item

    return clean(value, depth=0)  # type: ignore[return-value]


def _purpose_alias(purpose: str) -> str:
    normalized = str(purpose or "research").strip().casefold().replace("-", "_")
    aliases = {
        "research_component": "research",
        "research": "research",
        "form_assist": "form_assist",
        "form": "form_assist",
        "chat": "chat",
        "strategy": "strategy",
        "trade_plan": "strategy",
        "exit_advice": "strategy",
        "monitoring": "monitoring",
        "monitor": "monitoring",
    }
    return aliases.get(normalized, normalized)


def _default_profile(model: str) -> ModelRuntimeProfile:
    """Choose a safe protocol profile for an implicit model configuration.

    Older configuration rows intentionally leave ``model_profiles`` empty.  Treating
    every such model as generic meant the application's default GPT configuration
    silently omitted Responses continuation and prompt-cache controls.  Explicit
    profiles still take precedence; this only supplies the missing default.
    """

    normalized = model.strip().casefold()
    if normalized.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
        policy: CachePolicy = "OPENAI"
    elif normalized.startswith("grok-"):
        policy = "GROK"
    else:
        policy = "COMPATIBLE"
    return ModelRuntimeProfile(model=model, cache_policy=policy)


def _profile_storage_dict(profile: ModelRuntimeProfile) -> dict[str, Any]:
    return profile.public_dict()


def _normalize_profiles(
    raw_profiles: tuple[ModelProfileDraft, ...] | list[ModelProfileDraft],
    *,
    models: tuple[str, ...],
) -> tuple[ModelRuntimeProfile, ...]:
    by_model: dict[str, ModelRuntimeProfile] = {}
    for raw in raw_profiles:
        profile = ModelRuntimeProfile(**raw.__dict__)
        normalized_name = profile.model.strip()
        if not normalized_name:
            raise ModelSettingsError("model profile model is required")
        if normalized_name in by_model:
            raise ModelSettingsError("model profile model values must be unique")
        if profile.cache_policy not in {"GROK", "OPENAI", "COMPATIBLE"}:
            raise ModelSettingsError("model profile cache_policy is invalid")
        if not 1024 <= profile.context_window_tokens <= 4_000_000:
            raise ModelSettingsError("model profile context_window_tokens is invalid")
        if min(profile.output_token_reserve, profile.reasoning_token_reserve) < 0:
            raise ModelSettingsError("model profile token reserves must be non-negative")
        if profile.input_budget_tokens <= 0:
            raise ModelSettingsError("model profile reserves must leave input capacity")
        prices = (
            profile.input_price_per_million,
            profile.cached_input_price_per_million,
            profile.cache_write_price_per_million,
            profile.output_price_per_million,
        )
        if any(price < 0 for price in prices):
            raise ModelSettingsError("model profile prices must be non-negative")
        by_model[normalized_name] = ModelRuntimeProfile(
            **{**profile.__dict__, "model": normalized_name}
        )
    for model in dict.fromkeys(models):
        by_model.setdefault(model, _default_profile(model))
    return tuple(by_model[name] for name in sorted(by_model))


def _profiles_from_storage(
    raw_profiles: object, *, models: tuple[str, ...]
) -> tuple[ModelRuntimeProfile, ...]:
    profiles: list[ModelProfileDraft] = []
    if isinstance(raw_profiles, list):
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                continue
            try:
                profiles.append(
                    ModelProfileDraft(
                        model=str(raw["model"]),
                        cache_policy=str(raw.get("cache_policy", "COMPATIBLE")),  # type: ignore[arg-type]
                        context_window_tokens=int(raw.get("context_window_tokens", 128000)),
                        output_token_reserve=int(raw.get("output_token_reserve", 8192)),
                        reasoning_token_reserve=int(raw.get("reasoning_token_reserve", 0)),
                        input_price_per_million=Decimal(str(raw.get("input_price_per_million", 0))),
                        cached_input_price_per_million=Decimal(
                            str(raw.get("cached_input_price_per_million", 0))
                        ),
                        cache_write_price_per_million=Decimal(
                            str(raw.get("cache_write_price_per_million", 0))
                        ),
                        output_price_per_million=Decimal(
                            str(raw.get("output_price_per_million", 0))
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return _normalize_profiles(profiles, models=models)


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


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, OpenAICompatibleError):
        return exc.code
    if isinstance(exc, httpx.TimeoutException):
        return "MODEL_TIMEOUT"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 429:
            return "MODEL_RATE_LIMITED"
        if status_code in {408, 504}:
            return "MODEL_TIMEOUT"
        if status_code >= 500:
            return "MODEL_GATEWAY_UNAVAILABLE"
        return "MODEL_GATEWAY_ERROR"
    if isinstance(exc, httpx.RequestError):
        return "MODEL_GATEWAY_UNAVAILABLE"
    return "MODEL_SETTINGS_ERROR"
