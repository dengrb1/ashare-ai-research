from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_declares_control_plane_workers_and_object_bucket_init() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {
        "web",
        "api",
        "worker",
        "backtest-worker",
        "research-worker",
        "postgres",
        "redis",
        "minio",
        "minio-init",
    } <= set(services)
    assert services["api"]["depends_on"]["minio-init"]["condition"] == (
        "service_completed_successfully"
    )
    assert "healthcheck" in services["api"]
    assert "healthcheck" in services["web"]
    assert "healthcheck" in services["postgres"]
    assert "healthcheck" in services["redis"]
    assert "healthcheck" in services["minio"]
    assert ".env.docker" in services["api"]["env_file"]
    assert "host.docker.internal:host-gateway" in services["research-worker"]["extra_hosts"]
    assert services["redis"]["ports"] == ["6379:6379"]


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
    assert "redis://127.0.0.1:6379/0" in local
    assert "http://127.0.0.1:9000" in local
    assert "@postgres:5432/ashare" in docker
    assert "redis://redis:6379/0" in docker
    assert "http://minio:9000" in docker
    assert "http://host.docker.internal:3688/v1" in docker
    assert "MARKET_KLINE_CACHE_SECONDS=300" in local
    assert "MARKET_PREFETCH_MAX_WORKERS=4" in docker


def test_container_install_uses_dependency_lock() -> None:
    dockerfile = (ROOT / "docker" / "app.Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock" in dockerfile
    assert "pip install --requirement requirements.lock" in dockerfile
    assert "NEODATA_FINANCIAL_SEARCH_COMMIT=" in dockerfile
    assert "NEODATA_FINANCIAL_SEARCH_SHA256=" in dockerfile
    assert "NEODATA_FINANCIAL_SEARCH_PATH=/opt/neodata-financial-search/query.py" in dockerfile
    assert (ROOT / "requirements.lock").stat().st_size > 100


def test_web_container_builds_vite_assets_and_nginx_proxies_api() -> None:
    dockerfile = (ROOT / "web" / "Dockerfile").read_text(encoding="utf-8")
    nginx = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    assert "try_files $uri $uri/ /index.html" in nginx
    assert "proxy_pass http://api:8000" in nginx


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
