from __future__ import annotations

import json
import subprocess
import sys
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
    assert services["api"]["mem_limit"] == "384m"
    assert "healthcheck" in services["api"]
    assert "healthcheck" in services["web"]
    assert "healthcheck" in services["postgres"]
    assert "healthcheck" in services["redis"]
    assert ".env.docker" in services["api"]["env_file"]
    assert "host.docker.internal:host-gateway" in services["job-worker"]["extra_hosts"]
    web_loopback = "${WEB_BIND_ADDRESS:-127.0.0.1}"
    api_loopback = "${API_BIND_ADDRESS:-127.0.0.1}"
    service_loopback = "${SERVICE_BIND_ADDRESS:-127.0.0.1}"
    assert services["web"]["ports"] == [f"{web_loopback}:80:80"]
    assert services["api"]["ports"] == [f"{api_loopback}:8000:8000"]
    assert services["postgres"]["ports"] == [f"{service_loopback}:5432:5432"]
    assert services["redis"]["ports"] == [f"{service_loopback}:6379:6379"]


def test_optional_edge_gateway_is_isolated_and_memory_bounded() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    edge = compose["services"]["edge-gateway"]
    assert edge["profiles"] == ["edge"]
    assert edge["mem_limit"] == "96m"
    assert edge["pids_limit"] == 128
    assert edge["read_only"] is True
    assert "ports" not in edge
    assert edge["depends_on"] == {"web": {"condition": "service_healthy"}}
    assert edge["environment"]["EDGE_FRPC_ENABLED"] == "${EDGE_FRPC_ENABLED:-false}"
    assert edge["environment"]["EDGE_GATEWAY_RELEASE"] == "alpha"
    assert edge["labels"]["io.ashare.edge-gateway.release"] == "alpha"
    assert edge["build"]["args"]["EDGE_GATEWAY_VERSION"] == "${EDGE_GATEWAY_VERSION:-2.0.4-alpha.1}"
    assert edge["security_opt"] == ["no-new-privileges:true"]
    assert "NET_BIND_SERVICE" in edge["cap_add"]
    assert "edge-acme-data:/var/lib/acme" in edge["volumes"]
    assert "edge-certificates:/etc/edge/certs" in edge["volumes"]
    assert "edge-gateway-logs:/var/log/edge" in edge["volumes"]
    assert "/var/lib/acme-webroot:rw,noexec,nosuid,size=4m" in edge["tmpfs"]


def test_edge_gateway_pins_downloads_and_sanitizes_forwarded_headers() -> None:
    dockerfile = (ROOT / "docker" / "edge-gateway.Dockerfile").read_text(encoding="utf-8")
    nginx = (ROOT / "docker" / "edge-gateway" / "edge.conf.template").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "edge-gateway" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "FRP_VERSION=0.68.0" in dockerfile
    assert "FRP_SHA256=" in dockerfile
    assert "ACME_SH_VERSION=3.1.1" in dockerfile
    assert "ACME_SH_SHA256=" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "chown -R nginx:nginx /var/lib/acme" not in dockerfile
    assert "chown -R nginx:nginx /var/cache/nginx" in dockerfile
    assert "EDGE_GATEWAY_VERSION=2.0.4-alpha.1" in dockerfile
    assert 'org.opencontainers.image.version="${EDGE_GATEWAY_VERSION}"' in dockerfile
    assert "sed -i 's/\\r$//' /usr/local/bin/edge-gateway-entrypoint" in dockerfile
    assert "ssl_protocols TLSv1.2 TLSv1.3" in nginx
    assert "ssl_reject_handshake on" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "proxy_buffering off" in nginx
    assert "--keylength ec-256" in entrypoint
    assert "EDGE_FRPC_ENABLED=true requires" in entrypoint
    assert "using /tmp/edge logs until the container is recreated" in entrypoint
    assert "if ! mkdir -p \"$LOG_DIR\" 2>/dev/null || [ ! -w \"$LOG_DIR\" ]; then" in entrypoint
    assert 'chown -R nginx:nginx "$ACME_HOME"' not in entrypoint
    assert "chown -R nginx:nginx /tmp/client_temp" in entrypoint


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


def test_native_windows_entry_is_external_and_checksum_verified() -> None:
    native = ROOT / "scripts" / "native"
    lock = json.loads((native / "dependencies.lock.json").read_text(encoding="utf-8"))
    assert lock["platform"] == "windows-amd64"
    assert {item["id"] for item in lock["artifacts"]} == {
        "postgres",
        "redis-compatible",
        "searxng",
    }
    searxng = next(item for item in lock["artifacts"] if item["id"] == "searxng")
    assert len(searxng["commit"]) == 40
    installer = (native / "ashare-native.ps1").read_text(encoding="utf-8")
    assert "must be outside the source checkout" in installer
    assert "Get-FileHash -Algorithm SHA256" in installer
    assert "NATIVE_PROCESS_GROUP" in installer
    assert "WorkingSet64" in installer
    assert "Start-Process" in installer
    assert "AshareAIService" in installer
    assert "LogonType Password" in installer
    assert "RunLevel Limited" in installer
    assert "Protect-NativeRuntime" in installer
    assert '"*S-1-5-18:(OI)(CI)F"' in installer
    assert 'UserId "SYSTEM"' not in installer
    assert "native-ports.json" in installer
    assert "pwd.py" in installer
    assert "postgres.exe" in installer
    assert "Wait-PostgresReady" in installer
    assert "pg_ctl.exe" in installer
    assert (native / "ashare-native.cmd").is_file()
    assert "New-Item -ItemType Directory -Force -Path $logDirectory" in installer
    assert '"status" { Invoke-Status; exit 0 }' in installer
    assert "Get-NativeInstallationState" in installer
    assert "Test-NativeRuntimeHealthyFast" in installer
    assert "-Fast" in installer
    gui = ROOT / "windows" / "native-control-center"
    assert (gui / "Program.cs").is_file()
    assert (gui / "CommandSupport.cs").is_file()
    assert (gui / "Cli.cs").is_file()
    assert (gui / "ashareai.cmd").is_file()
    assert "System.Windows.Forms" in (gui / "Program.cs").read_text(encoding="utf-8")
    assert "JavaScriptSerializer" in (gui / "Program.cs").read_text(encoding="utf-8")
    program = (gui / "Program.cs").read_text(encoding="utf-8")
    assert "AshareAI.Controller" in program
    assert "AshareAI 本机运行管理器" in program
    assert "UseWaitCursor = true" not in program
    assert "--auto-install" in program
    assert "operation != \"status\"" in program
    assert "queuedOperation" in program
    assert "requireAdministrator" in (gui / "app.manifest").read_text(encoding="utf-8")
    assert (gui / "build.ps1").is_file()
    assert (gui / "Installer.cs").is_file()
    assert (gui / "setup.manifest").is_file()
    build = (gui / "build.ps1").read_text(encoding="utf-8")
    assert "AshareAI.Payload" in build
    assert "AshareAI.NativeControlCenter.Cli.exe" in build
    command_support = (gui / "CommandSupport.cs").read_text(encoding="utf-8")
    commands = ("install", "start", "stop", "restart", "repair", "status", "doctor", "open", "logs")
    for command in commands:
        assert f'"{command}"' in command_support
    installer_cs = (gui / "Installer.cs").read_text(encoding="utf-8")
    assert "/quiet" in installer_cs
    assert "/start-services" in installer_cs
    assert "QuietUninstallString" in installer_cs
    assert "/no-install-deps" in installer_cs
    assert len(searxng["sha256"]) == 64
    assert searxng["archive_url"].endswith(f"{searxng['commit']}.zip")
    assert not (native / "gui.cmd").exists()
    assert (ROOT / "docs" / "NATIVE_WINDOWS.md").is_file()
    linux_gui = ROOT / "linux" / "native-control-center"
    assert (linux_gui / "native_control_center.py").is_file()
    assert (linux_gui / "ashare-native-linux.sh").is_file()
    assert (linux_gui / "README.md").is_file()
    linux_gui_text = (linux_gui / "native_control_center.py").read_text(encoding="utf-8")
    assert "DEFAULT_CONTROLLER" in linux_gui_text
    assert "subprocess.run" in linux_gui_text
    linux_controller = (linux_gui / "ashare-native-linux.sh").read_text(encoding="utf-8")
    assert "status_json" in linux_controller
    assert "docker compose" not in linux_controller.lower()
    assert "docker run" not in linux_controller.lower()
    assert (linux_gui / "native_controller.py").is_file()


def test_linux_native_status_is_fast_and_safe_before_install(tmp_path: Path) -> None:
    controller = ROOT / "linux" / "native-control-center" / "native_controller.py"
    result = subprocess.run(
        [
            sys.executable,
            str(controller),
            "status",
            "--root",
            str(tmp_path),
            "--source-root",
            str(ROOT),
            "--json",
            "--fast",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["desired_state"] == "STOPPED"
    assert report["runtime_healthy"] is False
    assert report["installation"]["status"] == "NOT_INSTALLED"
    assert set(report["ports"]) == {"postgres", "redis", "api", "searxng"}
    assert "T" in report["collected_at"]


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
