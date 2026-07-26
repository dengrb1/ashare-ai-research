from __future__ import annotations

import json
from pathlib import Path

import yaml

from ashare_ai.observability.runtime_resources import DEFAULT_WORKER_LIMIT_BYTES, MIB

ROOT = Path(__file__).resolve().parents[2]


def test_compose_declares_low_memory_control_plane() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {
        "web",
        "api",
        "job-worker",
        "worker",
        "backtest-worker",
        "research-worker",
        "postgres",
        "redis",
    } <= set(services)
    assert "minio" not in services["api"]["depends_on"]
    assert "minio" not in services
    assert "minio-init" not in services
    assert services["research-worker"]["profiles"] == ["dual-research"]
    assert services["research-worker"]["scale"] == 2
    assert "healthcheck" in services["research-worker"]
    assert services["job-worker"]["mem_limit"] == "700m"
    assert services["research-worker"]["mem_limit"] == "700m"
    assert DEFAULT_WORKER_LIMIT_BYTES == 700 * MIB
    assert "scale" not in services["job-worker"]
    assert services["api"]["mem_limit"] == "320m"
    assert "healthcheck" in services["api"]
    assert "healthcheck" in services["web"]
    assert "healthcheck" in services["postgres"]
    assert "healthcheck" in services["redis"]
    assert ".env.docker" in services["api"]["env_file"]
    assert "host.docker.internal:host-gateway" in services["job-worker"]["extra_hosts"]
    web_loopback = "${WEB_BIND_ADDRESS:-127.0.0.1}"
    service_loopback = "${SERVICE_BIND_ADDRESS:-127.0.0.1}"
    assert services["web"]["ports"] == [f"{web_loopback}:80:80"]
    assert services["api"]["ports"] == [f"{service_loopback}:8000:8000"]
    assert services["postgres"]["ports"] == [f"{service_loopback}:5432:5432"]
    assert services["redis"]["ports"] == [f"{service_loopback}:6379:6379"]


def test_local_and_docker_environment_templates_are_separated() -> None:
    local = (ROOT / ".env.local.example").read_text(encoding="utf-8")
    docker = (ROOT / ".env.docker.example").read_text(encoding="utf-8")
    for factory in (
        "ASHARE_PIPELINE_FACTORY=ashare_ai.orchestration.production:create_pipeline",
        "ASHARE_STAGE_BACKEND_FACTORY=ashare_ai.orchestration.builtin:create_backend",
        "ASHARE_BACKTEST_EXECUTOR_FACTORY=ashare_ai.orchestration.builtin_backtest:create_executor",
    ):
        assert factory in local
        assert factory in docker
    assert "@127.0.0.1:5432/ashare" in local
    assert "WEB_BIND_ADDRESS=127.0.0.1" in local
    assert "SERVICE_BIND_ADDRESS=127.0.0.1" in local
    assert "AUTH_LOGIN_RATE_LIMIT_PER_MINUTE=10" in local
    assert "redis://:change-this-local-redis-password@127.0.0.1:6379/0" in local
    assert "OBJECT_STORE_ENDPOINT=\n" in local
    assert "${POSTGRES_PASSWORD}@postgres:5432/ashare" in docker
    assert "redis://:${REDIS_PASSWORD}@redis:6379/0" in docker
    assert "APP_ENV=" not in docker
    assert "OBJECT_STORE_ENDPOINT=\n" in docker
    assert "AGENT_BACKEND=" not in docker
    assert "LLM_BASE_URL=" not in docker
    assert "MODEL_SETTINGS_ENCRYPTION_KEYS=" in local
    assert "MARKET_KLINE_CACHE_SECONDS=300" in local
    assert "MARKET_PREFETCH_MAX_WORKERS=4" in docker
    assert "AKSHARE_FETCH_MAX_ATTEMPTS=2" in local
    assert "AKSHARE_FETCH_BACKOFF_SECONDS=1" in docker


def test_container_install_uses_dependency_lock() -> None:
    dockerfile = (ROOT / "docker" / "app.Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock" in dockerfile
    assert "pip install --no-cache-dir --requirement requirements.runtime.lock" in dockerfile
    assert "NEODATA_FINANCIAL_SEARCH_COMMIT=" in dockerfile
    assert "NEODATA_FINANCIAL_SEARCH_SHA256=" in dockerfile
    assert "NEODATA_FINANCIAL_SEARCH_PATH=/opt/neodata-financial-search/query.py" in dockerfile
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    runtime_lock = (ROOT / "requirements.runtime.lock").read_text(encoding="utf-8")
    assert len(lock) > 100
    assert "prefect==" not in runtime_lock
    assert "pytest==" not in runtime_lock
    assert "redis==" in runtime_lock


def test_postgres_runtime_replaces_vulnerable_gosu_binary() -> None:
    dockerfile = (ROOT / "docker" / "postgres.Dockerfile").read_text(encoding="utf-8")
    assert "su-exec" in dockerfile
    assert "rm -f /usr/local/bin/gosu" in dockerfile


def test_web_container_builds_vite_assets_and_nginx_proxies_api() -> None:
    dockerfile = (ROOT / "web" / "Dockerfile").read_text(encoding="utf-8")
    nginx = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    assert "try_files $uri $uri/ /index.html" in nginx
    assert "location = /assets {" in nginx
    assert "location = /assets/ {" in nginx
    assert "proxy_pass http://api:8000" in nginx or (
        "resolver 127.0.0.11" in nginx
        and "set $api_upstream api:8000" in nginx
        and "proxy_pass http://$api_upstream" in nginx
    )
    assert 'add_header X-Content-Type-Options "nosniff" always' in nginx
    assert 'add_header X-Frame-Options "DENY" always' in nginx
    assert "Content-Security-Policy" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "proxy_hide_header X-Content-Type-Options" in nginx


def test_first_release_policy_fixes_required_constraints() -> None:
    policy = json.loads((ROOT / "configs" / "first_release.v1.json").read_text(encoding="utf-8"))
    assert policy["scoring"] == {
        "formula_version": "composite-35-35-20-10-v1",
        "fundamental_weight": 0.35,
        "technical_weight": 0.35,
        "sentiment_weight": 0.2,
        "quality_confidence_weight": 0.1,
    }
    assert policy["portfolio"]["target_count"] == 15
    assert policy["portfolio"]["maximum_single_weight"] == 0.08
    assert policy["portfolio"]["maximum_industry_weight"] == 0.25
    assert policy["portfolio"]["maximum_one_way_turnover"] == 0.2
    assert policy["backtest"]["required_benchmarks"] == [
        "CSI300",
        "CSI500",
        "EQUAL_WEIGHT_UNIVERSE",
    ]
