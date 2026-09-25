"""Headless lifecycle controller for the A-Share AI Trader stack.

The tkinter application deliberately contains no process-management policy.  This
module owns the boring (and important) parts: locating PowerShell, starting and
stopping the shared lifecycle scripts, tailing their output without waiting on
child processes, and probing every service that the stack starts.

It is intentionally standard-library-only so it can be bundled by PyInstaller and
used from a future tray app, CLI, or test harness.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

# The desktop console is the primary lifecycle entry point.  The shared
# PowerShell controller remains the single implementation for CLI, installer,
# and GUI operations so every entry point uses the same PID state and ownership
# checks.
LIFECYCLE_SCRIPT = "scripts/service_control.ps1"
# Keep these names as compatibility aliases for callers importing the previous
# constants; both now resolve to the unified lifecycle controller.
START_SCRIPT = LIFECYCLE_SCRIPT
STOP_SCRIPT = LIFECYCLE_SCRIPT
LLM_SCRIPT = "scripts/llm_stack.ps1"

SERVICE_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("api", "Control API", "http://127.0.0.1:8000/api/health"),
    ("gateway", "Model Gateway", "http://127.0.0.1:8787/health/ready"),
    ("local_model", "Local Model", "http://127.0.0.1:8080/health"),
    ("quote_bridge", "Quote Bridge", "http://127.0.0.1:8081/health"),
)


DEFAULT_PORTS: dict[str, int] = {
    "api": 8000,
    "gateway": 8787,
    "local_model": 8080,
    "quote_bridge": 8081,
}


def _read_runtime_settings(repo_root: Path | None) -> dict[str, object]:
    """Read only the small runtime slice needed by the desktop probe."""

    if repo_root is None:
        return {}
    path = Path(repo_root) / "config" / "settings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _configured_port(settings: Mapping[str, object], name: str) -> int:
    system = settings.get("system")
    system = system if isinstance(system, Mapping) else {}
    default_name = "api" if name == "control_api" else name
    try:
        value = int(system.get(f"{name}_port", DEFAULT_PORTS[default_name]))
        return value if 1 <= value <= 65535 else DEFAULT_PORTS[default_name]
    except (TypeError, ValueError):
        return DEFAULT_PORTS[default_name]


def service_definitions(repo_root: Path | None = None) -> tuple[tuple[str, str, str], ...]:
    """Return the four existing probes, honoring configured component ports."""

    settings = _read_runtime_settings(repo_root)
    return (
        ("api", "Control API", f"http://127.0.0.1:{_configured_port(settings, 'control_api')}/api/health"),
        ("gateway", "Model Gateway", f"http://127.0.0.1:{_configured_port(settings, 'gateway')}/health/ready"),
        ("local_model", "Local Model", f"http://127.0.0.1:{_configured_port(settings, 'local_model')}/health"),
        ("quote_bridge", "Quote Bridge", f"http://127.0.0.1:{_configured_port(settings, 'quote_bridge')}/health"),
    )


def find_repo_root(start: Path | None = None) -> Path | None:
    """Find a repository containing the unified lifecycle controller."""

    current = Path(start) if start is not None else Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    current = current.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / LIFECYCLE_SCRIPT).is_file():
            return candidate
    return None


def shell_command() -> list[str]:
    """Return a non-interactive PowerShell command, preferring pwsh."""

    for name in ("pwsh", "powershell"):
        resolved = shutil.which(name)
        if resolved:
            return [resolved, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    raise RuntimeError("未找到 pwsh / powershell，无法执行启停脚本")


def decode_log_bytes(data: bytes) -> str:
    """Best-effort decoding for PowerShell output from different Windows locales."""

    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class HealthSnapshot:
    """A point-in-time health result for all managed services."""

    services: Mapping[str, Optional[dict[str, object]]]
    checked_at: datetime
    gateway: Mapping[str, object] = field(default_factory=dict)

    def payload(self, service: str) -> Optional[dict[str, object]]:
        return self.services.get(service)

    def is_ok(self, service: str) -> bool:
        payload = self.payload(service)
        if not isinstance(payload, dict):
            return False
        status = str(payload.get("status", "")).lower()
        return status in {"ok", "ready", "healthy", "running"} or (
            service == "api" and payload.get("status") == "ok"
        )

    @property
    def running(self) -> bool:
        return self.is_ok("api") or self.is_ok("gateway")

    @property
    def all_core_ok(self) -> bool:
        return self.is_ok("api") and self.is_ok("gateway")

    @property
    def mode(self) -> str:
        payload = self.payload("api")
        if isinstance(payload, dict):
            return str(payload.get("mode") or "-")
        return "-"

    @property
    def gateway_status(self) -> Mapping[str, object]:
        return self.gateway


@dataclass(frozen=True)
class OperationResult:
    action: str
    exit_code: int | None
    elapsed_seconds: float
    error: str | None = None


EventSink = Callable[[str, object], None]


class StackController:
    """Thread-safe, headless controller for the PowerShell-managed stack."""

    def __init__(
        self,
        repo_root: Path | None,
        event_sink: EventSink,
        logger: logging.Logger | None = None,
    ) -> None:
        self.repo_root = repo_root
        self._event_sink = event_sink
        self._logger = logger or logging.getLogger(__name__)
        self._shell: list[str] = []
        self._busy_lock = threading.Lock()
        self._busy_action: str | None = None
        self._health_lock = threading.Lock()
        self._health_pending = False
        self._closed = False

    @property
    def busy_action(self) -> str | None:
        with self._busy_lock:
            return self._busy_action

    @property
    def busy(self) -> bool:
        return self.busy_action is not None

    def set_repo_root(self, repo_root: Path | None) -> None:
        self.repo_root = repo_root

    def start(self, full_build: bool = False) -> bool:
        args = ["-Action", "Start", "-Services", "All"]
        if not full_build:
            args.append("-SkipBuild")
        return self._schedule("启动全部服务", LIFECYCLE_SCRIPT, args)

    def stop(self) -> bool:
        return self._schedule(
            "停止全部服务",
            LIFECYCLE_SCRIPT,
            ["-Action", "Stop", "-Services", "All"],
        )

    def restart(self, full_build: bool = False) -> bool:
        # Restart all services as one transition so the local model cannot be
        # left behind while the API/Gateway stack is being replaced.
        args = ["-Action", "Restart", "-Services", "All"]
        if not full_build:
            args.append("-SkipBuild")
        return self._schedule("重启全部服务", LIFECYCLE_SCRIPT, args)

    def start_local_model(self) -> bool:
        return self._schedule("启动本地模型", LLM_SCRIPT, ["-Start"])

    def stop_local_model(self) -> bool:
        return self._schedule("停止本地模型", LLM_SCRIPT, ["-Stop"])

    def local_model_status(self) -> bool:
        return self._schedule("检查本地模型", LLM_SCRIPT, ["-Status"])

    def _schedule(self, label: str, script_rel: str, args: list[str]) -> bool:
        if self.repo_root is None:
            self._emit("error", "尚未选择仓库目录")
            return False
        with self._busy_lock:
            if self._busy_action is not None:
                self._emit("warn", f"系统正在{self._busy_action}，请等待完成后再操作…")
                return False
            self._busy_action = label
        script = self.repo_root / script_rel
        if not script.is_file():
            with self._busy_lock:
                self._busy_action = None
            self._emit("error", f"未找到脚本: {script}")
            return False
        self._emit("busy", label)
        self._emit("log", f">>> {label}系统: {script} {' '.join(args) or '(跳过构建)'}")
        self._log(logging.INFO, f"{label}系统: {script} {' '.join(args)}")
        threading.Thread(
            target=self._run_script_worker,
            args=(script, args, label),
            daemon=True,
            name=f"stack-{label}",
        ).start()
        return True

    def _run_script_worker(self, script: Path, args: list[str], label: str) -> None:
        reader: threading.Thread | None = None
        stop = threading.Event()
        log_path: str | None = None
        started = time.monotonic()
        code: int | None = None
        error: str | None = None
        try:
            shell = self._shell or shell_command()
            self._shell = shell
            fd, log_path = tempfile.mkstemp(prefix="console_script_", suffix=".log")
            os.close(fd)
            with open(log_path, "wb") as output:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.Popen(
                    [*shell, "-File", str(script), *args],
                    cwd=str(self.repo_root),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            reader = threading.Thread(
                target=tail_file,
                args=(log_path, stop, self._event_sink),
                daemon=True,
                name=f"stack-log-{label}",
            )
            reader.start()
            code = proc.wait()
            if code == 0:
                self._emit("log", f">>> {label}脚本执行完成 (exit 0)")
                self._log(logging.INFO, f"{label}脚本执行完成 (exit 0)")
            else:
                self._emit("error", f">>> {label}脚本失败 (exit {code})")
                self._log(logging.ERROR, f"{label}脚本失败 (exit {code})")
        except Exception as exc:  # noqa: BLE001 - controller must report all failures
            error = str(exc)
            self._emit("error", f">>> 执行异常: {error}")
            self._log(logging.ERROR, f"{label}脚本执行异常: {error}")
        finally:
            stop.set()
            if reader is not None:
                reader.join(timeout=2.0)
            if log_path is not None:
                try:
                    os.remove(log_path)
                except OSError:
                    pass
            with self._busy_lock:
                self._busy_action = None
            result = OperationResult(label, code, time.monotonic() - started, error)
            self._emit("done", result)
            self._log(logging.INFO, f"{label}流程结束")

    def refresh_health(self) -> bool:
        with self._health_lock:
            if self._health_pending or self._closed:
                return False
            self._health_pending = True
        threading.Thread(target=self._fetch_health, daemon=True, name="stack-health").start()
        return True

    def _fetch_health(self) -> None:
        payloads: dict[str, Optional[dict[str, object]]] = {}
        try:
            # Probes are independent; do them concurrently so one unavailable
            # bridge cannot delay the status of the other three services.
            with ThreadPoolExecutor(
                max_workers=len(SERVICE_DEFINITIONS), thread_name_prefix="probe"
            ) as pool:
                futures = {
                    service: pool.submit(fetch_json, url)
                    for service, _label, url in service_definitions(self.repo_root)
                }
                for service, future in futures.items():
                    try:
                        payloads[service] = future.result()
                    except Exception:  # noqa: BLE001 - a single probe fails closed
                        payloads[service] = None
            gateway = fetch_gateway_snapshot(self.repo_root)
            self._emit("health", HealthSnapshot(payloads, datetime.now(timezone.utc), gateway))
        finally:
            with self._health_lock:
                self._health_pending = False

    def close(self) -> None:
        self._closed = True

    def _emit(self, kind: str, value: object) -> None:
        try:
            self._event_sink(kind, value)
        except Exception:  # noqa: BLE001 - event delivery must not kill worker threads
            self._log(logging.ERROR, "控制器事件发送失败", exc_info=True)

    def _log(self, level: int, message: str, **kwargs: object) -> None:
        try:
            self._logger.log(level, message, **kwargs)
        except Exception:  # noqa: BLE001 - logging is best effort
            pass


def fetch_json(url: str, timeout: float = 2.5) -> dict[str, object] | None:
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        payload = json.loads(body)
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 - a health probe is allowed to fail closed
        return None


def _number(value: object, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value) if isinstance(value, str) else value
        if not isinstance(parsed, (int, float)):
            return None
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return None
        return int(parsed) if integer else parsed
    except (TypeError, ValueError):
        return None


def _channel_health(channel: Mapping[str, object]) -> bool:
    for key in ("probe_healthy", "healthy", "health"):
        if key in channel:
            value = channel.get(key)
            if isinstance(value, bool):
                return value
            if str(value).strip().lower() in {"ok", "ready", "healthy", "true", "1", "up"}:
                return True
            if value is not None:
                return False
    status = str(channel.get("status", "")).lower()
    return status in {"ok", "ready", "healthy", "running", "up"}


def _gateway_metadata(repo_root: Path | None, settings: Mapping[str, object] | None = None) -> dict[str, object]:
    settings = settings or _read_runtime_settings(repo_root)
    port = _configured_port(settings, "gateway")
    root = Path(repo_root) if repo_root is not None else None
    binary = root / "gateway" / "target" / "release" / "ashare-model-gateway.exe" if root else None
    market = settings.get("market")
    market = market if isinstance(market, Mapping) else {}
    configured_data = str(market.get("snapshot_dir") or "").strip()
    data_dir = Path(configured_data) if configured_data else (root / "models" / "data" if root else None)
    pid: int | None = None
    if root:
        try:
            state = json.loads((root / ".run" / "stack.json").read_text(encoding="utf-8"))
            if isinstance(state, dict):
                pid = _number(state.get("gateway_pid"), integer=True)  # type: ignore[assignment]
        except (OSError, ValueError, TypeError):
            pass
    return {
        "port": port,
        "pid": pid,
        "binary_path": str(binary) if binary is not None else None,
        "data_dir": str(data_dir) if data_dir is not None else None,
    }


def normalize_gateway_snapshot(
    payload: Mapping[str, object] | None,
    *,
    metadata: Mapping[str, object] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    """Normalize both control-plane and direct gateway status responses."""

    metadata = metadata or {}
    raw = payload if isinstance(payload, Mapping) else {}
    channels = raw.get("channels")
    if not isinstance(channels, list):
        channels = raw.get("providers")
    channel_items = [item for item in channels if isinstance(item, Mapping)] if isinstance(channels, list) else []
    total = _number(raw.get("channels_total"), integer=True)
    if total is None and channel_items:
        total = len(channel_items)
    healthy = _number(raw.get("channels_healthy"), integer=True)
    if healthy is None and channel_items:
        healthy = sum(1 for item in channel_items if _channel_health(item))
    status_error = raw.get("error")
    return {
        "reachable": bool(raw.get("reachable", payload is not None)) and payload is not None,
        "port": _number(raw.get("port"), integer=True) or metadata.get("port"),
        "version": str(raw.get("version")) if raw.get("version") not in (None, "") else None,
        "uptime_seconds": _number(raw.get("uptime_seconds"), integer=True),
        "channels_total": total,
        "channels_healthy": healthy,
        "route_model": str(raw.get("route_model")) if raw.get("route_model") not in (None, "") else None,
        "failovers": _number(raw.get("failovers"), integer=True),
        "pid": _number(raw.get("pid"), integer=True) or metadata.get("pid"),
        "binary_path": raw.get("binary_path") or metadata.get("binary_path"),
        "data_dir": raw.get("data_dir") or metadata.get("data_dir"),
        "error": str(error or status_error) if (error or status_error) else None,
    }


def _gateway_listen_host(repo_root: Path | None) -> str:
    if repo_root is None:
        return "127.0.0.1"
    path = Path(repo_root) / "gateway" / "config.toml"
    try:
        raw = path.read_text(encoding="utf-8")
        # The desktop only needs the host; avoid making TOML parsing a runtime
        # dependency in the controller.  The configured port comes from JSON.
        for line in raw.splitlines():
            if line.strip().startswith("listen") and "=" in line:
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                host = value.rsplit(":", 1)[0] if ":" in value else value
                return host or "127.0.0.1"
    except OSError:
        pass
    return "127.0.0.1"


def fetch_gateway_snapshot(repo_root: Path | None, timeout: float = 2.5) -> dict[str, object]:
    """Read /api/gateway first, then fall back to Gateway /admin/status."""

    settings = _read_runtime_settings(repo_root)
    metadata = _gateway_metadata(repo_root, settings)
    system = settings.get("system")
    system = system if isinstance(system, Mapping) else {}
    api_host = str(system.get("control_api_host") or "127.0.0.1").strip() or "127.0.0.1"
    api_port = _configured_port(settings, "control_api")
    control_url = f"http://{api_host}:{api_port}/api/gateway"
    control_payload = fetch_json(control_url, timeout=timeout)
    if control_payload is not None and bool(control_payload.get("reachable", True)):
        return normalize_gateway_snapshot(control_payload, metadata=metadata)

    gateway_url = f"http://{_gateway_listen_host(repo_root)}:{metadata['port']}/admin/status"
    direct_payload = fetch_json(gateway_url, timeout=timeout)
    if direct_payload is not None:
        return normalize_gateway_snapshot(direct_payload, metadata=metadata)

    error = "控制面和网关均不可达"
    if control_payload is not None:
        error = str(control_payload.get("error") or "网关返回不可达")
    return normalize_gateway_snapshot(None, metadata=metadata, error=error)


def tail_file(path: str, stop: threading.Event, event_sink: EventSink) -> None:
    """Tail a growing file until the producer is done and all bytes are read."""

    pending = ""
    try:
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(65536)
                if chunk:
                    pending += decode_log_bytes(chunk)
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        event_sink("log", line.rstrip("\r"))
                    if len(pending) > 65536:
                        event_sink("log", pending)
                        pending = ""
                elif stop.is_set():
                    if pending:
                        event_sink("log", pending)
                    return
                else:
                    time.sleep(0.15)
    except OSError:
        return


__all__ = [
    "HealthSnapshot",
    "OperationResult",
    "SERVICE_DEFINITIONS",
    "DEFAULT_PORTS",
    "LIFECYCLE_SCRIPT",
    "START_SCRIPT",
    "STOP_SCRIPT",
    "StackController",
    "decode_log_bytes",
    "find_repo_root",
    "fetch_gateway_snapshot",
    "shell_command",
    "normalize_gateway_snapshot",
    "service_definitions",
    "tail_file",
]
