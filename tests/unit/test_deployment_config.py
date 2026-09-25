from __future__ import annotations

import importlib.util
import json
import os
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
        "private-data-init",
        "gateway",
        "quote-bridge",
        "web",
        "api",
        "job-worker",
        "postgres",
        "redis",
    } <= set(services)
    assert not (
        {"worker", "backtest-worker", "research-worker", "exit-advice-worker"}
        & set(services)
    )
    assert "minio" not in services["api"]["depends_on"]
    assert "minio" not in services
    assert "minio-init" not in services
    assert services["job-worker"]["mem_limit"] == "700m"
    assert DEFAULT_WORKER_LIMIT_BYTES == 700 * MIB
    assert "scale" not in services["job-worker"]
    assert services["api"]["mem_limit"] == "384m"
    assert "healthcheck" in services["api"]
    assert "healthcheck" in services["web"]
    assert "healthcheck" in services["postgres"]
    assert "healthcheck" in services["redis"]
    assert ".env.docker" in services["api"]["env_file"]
    assert "host.docker.internal:host-gateway" in services["job-worker"]["extra_hosts"]
    assert services["gateway"]["mem_limit"] == "128m"
    assert "gateway" in services["api"]["depends_on"]
    assert "quote-bridge" in services["api"]["depends_on"]
    web_loopback = "${WEB_BIND_ADDRESS:-127.0.0.1}"
    api_loopback = "${API_BIND_ADDRESS:-127.0.0.1}"
    service_loopback = "${SERVICE_BIND_ADDRESS:-127.0.0.1}"
    assert services["web"]["ports"] == [f"{web_loopback}:80:80"]
    assert services["api"]["ports"] == [f"{api_loopback}:8000:8000"]
    assert services["postgres"]["ports"] == [f"{service_loopback}:5432:5432"]
    assert services["redis"]["ports"] == [f"{service_loopback}:6379:6379"]


def test_ghcr_deployment_pulls_every_custom_image() -> None:
    ghcr = yaml.safe_load((ROOT / "compose.ghcr.yaml").read_text(encoding="utf-8"))
    services = ghcr["services"]
    assert "ashare-ai-research:latest" in services["api"]["image"]
    assert "ashare-ai-research-web:latest" in services["web"]["image"]
    assert "ashare-ai-research-postgres:latest" in services["postgres"]["image"]
    assert "ashare-ai-research:latest" in services["job-worker"]["image"]
    for service in ("api", "web", "job-worker", "postgres"):
        assert services[service]["build"] is None

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
    assert "AKSHARE_FETCH_MAX_ATTEMPTS=3" in local
    assert "AKSHARE_FETCH_BACKOFF_SECONDS=5" in docker


def test_container_install_uses_dependency_lock() -> None:
    dockerfile = (ROOT / "docker" / "app.Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock" in dockerfile
    assert "pip install --no-cache-dir --requirement requirements.runtime.lock" in dockerfile
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
    assert {item["id"] for item in lock["artifacts"]} == {"postgres", "redis-compatible"}
    installer = (native / "ashare-native.ps1").read_text(encoding="utf-8")
    assert "must be outside the source checkout" in installer
    assert "Get-FileHash -Algorithm SHA256" in installer
    assert "NATIVE_PROCESS_GROUP" in installer
    assert "WorkingSet64" in installer
    assert "Start-Process" in installer
    assert "AshareAIService" in installer
    assert "-Password $servicePassword" in installer
    assert "RunLevel Limited" in installer
    assert "Protect-NativeRuntime" in installer
    assert '"*S-1-5-18:(OI)(CI)F"' in installer
    assert 'UserId "SYSTEM"' not in installer
    assert '@("task", "account", "system")' not in installer
    assert "native-ports.json" in installer
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
    assert "NotifyIcon" in program
    assert "MinimizeToTray" in program
    assert "--minimized" in program
    assert "StartupEntry" in program
    assert "requireAdministrator" in (gui / "app.manifest").read_text(encoding="utf-8")
    assert (gui / "build.ps1").is_file()
    assert (gui / "Installer.cs").is_file()
    assert (gui / "setup.manifest").is_file()
    build = (gui / "build.ps1").read_text(encoding="utf-8")
    assert "AshareAI.Payload" in build
    assert "AshareAI.NativeControlCenter.Cli.exe" in build
    assert "StartupIntegration.cs" in build
    command_support = (gui / "CommandSupport.cs").read_text(encoding="utf-8")
    commands = ("install", "start", "stop", "restart", "repair", "status", "doctor", "open", "logs")
    for command in commands:
        assert f'"{command}"' in command_support
    installer_cs = (gui / "Installer.cs").read_text(encoding="utf-8")
    assert "/quiet" in installer_cs
    assert "/start-services" in installer_cs
    assert "QuietUninstallString" in installer_cs
    assert "/no-install-deps" in installer_cs
    assert "CommonStartMenu" in installer_cs
    assert "StartupEntry.SetEnabled" in installer_cs
    assert "BuildVisibleArguments" in installer_cs
    assert "startup registration failed" in installer_cs
    assert "NoStartup" in installer_cs
    assert "Arguments" in installer_cs
    assert (gui / "StartupIntegration.cs").is_file()
    startup = (gui / "StartupIntegration.cs").read_text(encoding="utf-8")
    assert "Registry.CurrentUser" in startup
    assert "CurrentVersion\\Run" in startup
    assert (gui / "SingleInstance.cs").is_file()
    single_instance = (gui / "SingleInstance.cs").read_text(encoding="utf-8")
    assert "Local\\AshareAI.NativeControlCenter" in single_instance
    assert "Mutex" in single_instance
    assert "FindWindow" in single_instance
    assert "TryAcquire" in program
    assert "SingleInstance.cs" in build
    assert not (native / "gui.cmd").exists()
    assert (ROOT / "docs" / "NATIVE_WINDOWS.md").is_file()
    linux_gui = ROOT / "linux" / "native-control-center"
    assert (linux_gui / "native_control_center.py").is_file()
    assert (linux_gui / "ashare-native-linux.sh").is_file()
    assert (linux_gui / "README.md").is_file()
    linux_gui_text = (linux_gui / "native_control_center.py").read_text(encoding="utf-8")
    assert "DEFAULT_CONTROLLER" in linux_gui_text
    assert "subprocess.run" in linux_gui_text
    assert "pystray" in linux_gui_text
    assert "write_desktop_integration" in linux_gui_text
    assert "SingleInstance" in linux_gui_text
    assert "fcntl.flock" in linux_gui_text
    assert "SIGUSR1" in linux_gui_text
    assert "--minimized" in linux_gui_text
    assert (linux_gui / "ashare-native-console.sh").is_file()
    assert (linux_gui / "requirements.console.lock").is_file()
    assert "pillow==" in (linux_gui / "requirements.console.lock").read_text(encoding="utf-8")
    assert "pystray==" in (linux_gui / "requirements.console.lock").read_text(encoding="utf-8")
    package_builder = (linux_gui / "build-package.sh").read_text(encoding="utf-8")
    assert 'CONTROLLER_ARGS=(install --root "$ROOT" --source-root "$PACKAGE_DIR/app")' in package_builder
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
    assert set(report["ports"]) == {"postgres", "redis", "api"}
    assert "T" in report["collected_at"]


def test_linux_console_desktop_integration_contains_fixed_runtime_paths(
    tmp_path: Path, monkeypatch
) -> None:
    module_path = ROOT / "linux" / "native-control-center" / "native_control_center.py"
    spec = importlib.util.spec_from_file_location("ashare_native_console", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    application_path, autostart_path = module.write_desktop_integration(
        tmp_path / "ashare-native-console.sh",
        tmp_path / "ashare-native-linux.sh",
        tmp_path / "app",
        tmp_path / "runtime",
        True,
    )
    application = application_path.read_text(encoding="utf-8")
    autostart = autostart_path.read_text(encoding="utf-8")
    assert '"--root" "' in application
    assert str(tmp_path / "runtime").replace("\\", "\\\\") in application
    assert "--minimized" not in application
    assert "--minimized" in autostart
    assert autostart_path.is_file()

    module.write_desktop_integration(
        tmp_path / "ashare-native-console.sh",
        tmp_path / "ashare-native-linux.sh",
        tmp_path / "app",
        tmp_path / "runtime-2",
        False,
    )
    assert not autostart_path.exists()


def test_linux_console_single_instance_lock_is_exclusive(tmp_path: Path) -> None:
    module_path = ROOT / "linux" / "native-control-center" / "native_control_center.py"
    spec = importlib.util.spec_from_file_location("ashare_native_console_lock", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.fcntl is None:
        return

    lock_path = tmp_path / "console.lock"
    first = module.SingleInstance(lock_path)
    second = module.SingleInstance(lock_path)
    assert first.acquire()
    assert lock_path.read_text(encoding="ascii") == str(os.getpid())
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


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
