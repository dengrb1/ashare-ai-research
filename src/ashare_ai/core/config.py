from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def runtime_resource_path(relative_path: str) -> Path:
    """Return a bundled resource path when running as a PyInstaller binary."""

    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root) / relative_path
    return Path(relative_path)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite+pysqlite:///./data/ashare.db"
    redis_url: str = "redis://localhost:6379/0"
    lake_root: Path = Path("data/lake")
    private_object_root: Path = Path("data/private")
    native_web_root: Path | None = None
    policy_config_path: Path = runtime_resource_path("configs/first_release.v3.json")
    object_store_endpoint: str | None = None
    object_store_bucket: str = "ashare-research"
    object_store_access_key: str | None = None
    object_store_secret_key: str | None = None
    object_store_secure: bool = False
    tushare_token: str | None = None
    admin_username: str | None = None
    admin_password: str | None = None
    session_cookie_name: str = "ashare_session"
    csrf_cookie_name: str = "ashare_csrf"
    session_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=24 * 60)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)
    auth_login_rate_limit_per_minute: int = Field(default=10, ge=1, le=120)
    cookie_secure: bool = False
    trusted_hosts: str = "*"
    market_cache_seconds: int = Field(default=5, ge=1, le=300)
    market_kline_cache_seconds: int = Field(default=300, ge=15, le=3600)
    market_prefetch_max_workers: int = Field(default=4, ge=1, le=16)
    market_provider_max_workers: int = Field(default=4, ge=1, le=16)
    market_provider_max_queue: int = Field(default=8, ge=1, le=64)
    market_cache_max_entries: int = Field(default=512, ge=64, le=4096)
    market_stale_seconds: int = Field(default=900, ge=15, le=86_400)
    market_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    market_hedge_delay_seconds: float = Field(default=0.5, ge=0.1, le=3.0)
    financial_search_provider: str = "neodata-financial-search"
    neodata_financial_search_path: Path | None = None
    neodata_financial_search_mode: Literal["auto", "cli", "embedded"] = "auto"
    neodata_financial_search_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    financial_search_cache_seconds: int = Field(default=15, ge=0, le=300)
    financial_search_max_concurrency: int = Field(default=4, ge=1, le=32)
    financial_search_rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    searxng_base_url: str = "http://127.0.0.1:8080"
    searxng_timeout_seconds: float = Field(default=12.0, gt=0, le=60)
    searxng_max_results: int = Field(default=5, ge=1, le=10)
    ai_chat_rate_limit_per_minute: int = Field(default=10, ge=1, le=120)
    canonical_bundle_mode: Literal["akshare", "file", "demo"] = "akshare"
    allow_demo_data: bool = False
    akshare_bundle_size: int = Field(default=20, ge=15, le=100)
    akshare_history_sessions: int = Field(default=280, ge=65, le=500)
    akshare_fetch_max_attempts: int = Field(default=2, ge=1, le=5)
    akshare_fetch_backoff_seconds: float = Field(default=1.0, ge=0, le=60)
    worker_lease_seconds: int = Field(default=900, ge=30, le=7200)
    ashare_pipeline_factory: str | None = None
    ashare_stage_backend_factory: str | None = None
    ashare_backtest_executor_factory: str | None = None
    dependency_lock_path: Path = runtime_resource_path("requirements.lock")
    git_sha: str = "UNVERSIONED"
    log_level: str = "INFO"
    decision_hour: int = Field(default=18, ge=0, le=23)
    decision_minute: int = Field(default=0, ge=0, le=59)
    daily_research_start_hour: int = Field(default=15, ge=0, le=23)
    daily_research_start_minute: int = Field(default=5, ge=0, le=59)
    daily_research_retry_minutes: int = Field(default=5, ge=1, le=60)
    daily_research_retry_limit_minutes: int = Field(default=120, ge=5, le=24 * 60)
    minimum_listing_days: int = Field(default=120, ge=0)
    minimum_median_amount: float = Field(default=50_000_000.0, ge=0)
    agent_backend: Literal["builtin", "openai_compatible"] = "builtin"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gpt-5.6-sol"
    llm_reasoning_effort: str = "high"
    llm_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    # Per-run model-agent concurrency. The control plane validates the DUAL-mode
    # aggregate (two research workers) stays within MODEL_GATEWAY_MAX_CONCURRENCY,
    # so the upper bound here only mirrors the gateway headroom an operator can
    # raise.  The default keeps existing single-gateway deployments unchanged.
    llm_agent_max_concurrency: int = Field(default=4, ge=1, le=16)
    research_execution_mode: Literal["SERIAL", "DUAL"] = "SERIAL"
    edge_gateway_enabled: bool = False
    # Public HTTPS edge-gateway (nginx + acme.sh + optional frpc client)
    # settings.  The environment remains the deployment baseline, but every
    # value below is also administrator-overridable from the system settings
    # center; the host-side topology controller injects the persisted values
    # into the compose subprocess that (re)creates the edge-gateway service.
    edge_domain: str | None = None
    edge_acme_email: str | None = None
    edge_acme_ca_server: str = "letsencrypt"
    edge_frpc_enabled: bool = False
    edge_frpc_config_file: str = "./docker/edge-gateway/frpc.disabled.toml"
    # When enabled, the host-side topology controller force-recreates the
    # affected Compose services after a persisted topology change instead of
    # leaving the operator to run the restart command shown in the UI.
    auto_restart_enabled: bool = False
    # Deployment-only capability for the local host task which applies
    # Compose profiles.  It is never stored in system-configuration history.
    topology_controller_token: str | None = None
    # The model gateway is an infrastructure capacity, not a user-editable
    # model credential.  It remains environment/Compose managed and lets the
    # control plane fail closed before enabling two research consumers.
    model_gateway_max_concurrency: int = Field(default=8, ge=1, le=128)
    model_settings_encryption_keys: str | None = None
    edge_gateway_encryption_keys: str | None = None
    edge_gateway_config_dir: Path = Path(".secrets/edge-gateway")
    edge_gateway_source_dir: Path = Path("docker/edge-gateway")
    edge_gateway_host_source_dir: Path = Path("docker/edge-gateway")
    edge_proxy_target_allowlist: str = "web"
    personal_data_encryption_keys: str | None = None
    mipush_app_secret: str | None = None
    mipush_package_name: str | None = None
    mipush_api_url: str = "https://api.xmpush.xiaomi.com/v3/message/regid"
    model_allowed_hosts: str | None = None
    enable_prefect_flows: bool = False

    @field_validator("neodata_financial_search_path", mode="before")
    @classmethod
    def empty_neodata_path_is_unconfigured(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("edge_domain", mode="before")
    @classmethod
    def clean_edge_domain(cls, value: object) -> object:
        if value is None:
            return None
        domain = str(value).strip().lower().rstrip(".")
        if domain and (
            domain.startswith(("http://", "https://"))
            or "/" in domain
            or any(char.isspace() for char in domain)
        ):
            raise ValueError("edge domain must be a bare DNS host name without scheme or path")
        return domain or None

    @field_validator("edge_acme_email", mode="before")
    @classmethod
    def clean_edge_acme_email(cls, value: object) -> object:
        if value is None:
            return None
        email = str(value).strip()
        if email and ("@" not in email or any(char.isspace() for char in email)):
            raise ValueError("edge ACME account email must contain @ and no whitespace")
        return email or None

    @property
    def trusted_host_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @property
    def model_allowed_host_set(self) -> frozenset[str]:
        return frozenset(
            item.strip().casefold().rstrip(".")
            for item in (self.model_allowed_hosts or "").split(",")
            if item.strip()
        )

    def validate_production_security(self) -> None:
        """Fail closed when a production process is configured unsafely."""
        if self.app_env.casefold() != "production":
            return
        problems: list[str] = []
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true")
        if not self.trusted_host_list or "*" in self.trusted_host_list:
            problems.append("TRUSTED_HOSTS must contain explicit public host names")
        if not self.admin_username or not self.admin_password:
            problems.append("ADMIN_USERNAME and ADMIN_PASSWORD are required")
        elif len(self.admin_password) < 14 or self.admin_password.casefold() in {
            "change-this-before-starting",
            "password",
            "admin-password",
        }:
            problems.append("ADMIN_PASSWORD must be a non-default value of at least 14 characters")

        database = urlsplit(self.database_url)
        if not database.scheme.startswith("postgresql"):
            problems.append("DATABASE_URL must use PostgreSQL")
        if (
            not database.password
            or database.password.casefold() in {"ashare", "password", "postgres"}
            or database.password.casefold().startswith("change-this")
        ):
            problems.append("DATABASE_URL must contain a non-default database password")

        redis = urlsplit(self.redis_url)
        if redis.scheme not in {"redis", "rediss"} or not redis.password:
            problems.append("REDIS_URL must contain Redis authentication credentials")
        elif redis.password.casefold() in {"password", "redis", "local-redis-only"} or (
            redis.password.casefold().startswith("change-this")
        ):
            problems.append("REDIS_URL must contain a non-default Redis password")

        if self.object_store_endpoint:
            endpoint = urlsplit(self.object_store_endpoint)
            if endpoint.scheme != "https" or not self.object_store_secure:
                problems.append("external object-store endpoints must use HTTPS")
            if not self.object_store_access_key or not self.object_store_secret_key:
                problems.append(
                    "object-store credentials are required when an endpoint is configured"
                )
            elif (
                self.object_store_access_key.casefold() == "minioadmin"
                or self.object_store_secret_key.casefold() == "minioadmin"
            ):
                problems.append("object-store default credentials are forbidden")
        elif (self.object_store_access_key or self.object_store_secret_key) and (
            not self.object_store_secure
        ):
            problems.append("external object-store credentials require TLS")
        if not self.model_allowed_host_set:
            problems.append("MODEL_ALLOWED_HOSTS must explicitly allow model gateway hosts")
        if self.agent_backend == "openai_compatible" and not self.model_settings_encryption_keys:
            problems.append("MODEL_SETTINGS_ENCRYPTION_KEYS is required for the model backend")
        if not (self.personal_data_encryption_keys or self.model_settings_encryption_keys):
            problems.append(
                "PERSONAL_DATA_ENCRYPTION_KEYS is required for images and personal archives"
            )
        if bool(self.mipush_app_secret) != bool(self.mipush_package_name):
            problems.append("MIPUSH_APP_SECRET and MIPUSH_PACKAGE_NAME must be configured together")
        if self.mipush_app_secret and not self.mipush_api_url.startswith("https://"):
            problems.append("MIPUSH_API_URL must use HTTPS")
        if self.allow_demo_data or self.canonical_bundle_mode == "demo":
            problems.append("demo market data is forbidden in production")
        if problems:
            raise ValueError("unsafe production configuration: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
