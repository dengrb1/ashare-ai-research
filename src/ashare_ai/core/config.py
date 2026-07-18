from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite+pysqlite:///./data/ashare.db"
    redis_url: str = "redis://localhost:6379/0"
    lake_root: Path = Path("data/lake")
    policy_config_path: Path = Path("configs/first_release.v2.json")
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
    cookie_secure: bool = False
    market_cache_seconds: int = Field(default=15, ge=1, le=300)
    market_kline_cache_seconds: int = Field(default=300, ge=15, le=3600)
    market_prefetch_max_workers: int = Field(default=4, ge=1, le=16)
    market_stale_seconds: int = Field(default=900, ge=15, le=86_400)
    market_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    financial_search_provider: str = "neodata-financial-search"
    neodata_financial_search_path: Path | None = None
    neodata_financial_search_mode: Literal["auto", "cli", "embedded"] = "auto"
    neodata_financial_search_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    financial_search_cache_seconds: int = Field(default=15, ge=0, le=300)
    financial_search_max_concurrency: int = Field(default=4, ge=1, le=32)
    financial_search_rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
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
    dependency_lock_path: Path = Path("requirements.lock")
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
    model_settings_encryption_keys: str | None = None

    @field_validator("neodata_financial_search_path", mode="before")
    @classmethod
    def empty_neodata_path_is_unconfigured(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
