#!/usr/bin/env python3
"""Linux native runtime controller used by the Tkinter manager and shell entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_PORTS = {"postgres": 55432, "redis": 56379, "api": 58000, "searxng": 58080}
PORT_CANDIDATES = {
    "postgres": (55432, 55433, 55434, 55600, 55601),
    "redis": (56379, 56380, 56381, 55610, 55611),
    "api": (58000, 58001, 58002, 55620, 55621),
    "searxng": (58080, 58081, 58082, 55630, 55631),
}
COMMANDS = {"install", "start", "stop", "restart", "repair", "status", "doctor"}


class NativeError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: object) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def find_source_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        packaged_app = candidate / "app"
        if (packaged_app / "pyproject.toml").is_file() and (packaged_app / "src").is_dir():
            return packaged_app
        if (candidate / "pyproject.toml").is_file() and (candidate / "src").is_dir():
            return candidate
    return current


def process_alive(pid: object) -> bool:
    try:
        process_id = int(pid)
        os.kill(process_id, 0)
        return Path(f"/proc/{process_id}").exists()
    except (OSError, TypeError, ValueError):
        return False


def process_rss(pid: object) -> int:
    try:
        for line in Path(f"/proc/{int(pid)}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def find_binary(*names: str) -> str | None:
    for name in names:
        candidate = shutil.which(name)
        if candidate:
            return candidate
    return None


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@contextmanager
def management_lock(path: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - Linux always provides fcntl.
        raise NativeError("Linux file locking is unavailable") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NativeError("another native management operation is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Controller:
    def __init__(
        self, root: Path, source_root: Path, research_mode: str, workers: int, watchdog: int
    ) -> None:
        self.root = root.expanduser().resolve()
        self.source_root = source_root.expanduser().resolve()
        self.research_mode = research_mode
        self.workers = workers
        self.watchdog_interval = watchdog
        self.config = self.root / "config"
        self.state = self.root / "state"
        self.logs = self.root / "logs"
        self.env_path = self.root / ".env"
        self.paths_path = self.config / "native-paths.json"
        self.ports_path = self.config / "native-ports.json"
        self.processes_path = self.state / "processes.json"
        self.desired_path = self.state / "desired-state.json"
        self.watchdog_path = self.state / "watchdog.json"
        self.lock_path = self.state / "management.lock"

    def initialize(self) -> None:
        if is_inside(self.root, self.source_root):
            raise NativeError(
                f"ASHARE_NATIVE_ROOT must be outside the source checkout: {self.root}"
            )
        for directory in (
            self.root,
            self.config,
            self.state,
            self.logs,
            self.root / "data" / "postgres",
            self.root / "data" / "redis",
            self.root / "downloads",
            self.root / "deps",
            self.root / "web",
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def desired_state(self) -> str:
        value = read_json(self.desired_path, {})
        return str(value.get("desired_state", "STOPPED")) if isinstance(value, dict) else "STOPPED"

    def set_desired_state(self, value: str) -> None:
        write_json(self.desired_path, {"desired_state": value, "updated_at": utc_now()})

    def state_services(self) -> list[dict[str, object]]:
        value = read_json(self.processes_path, {})
        if not isinstance(value, dict) or not isinstance(value.get("services"), list):
            return []
        return [item for item in value["services"] if isinstance(item, dict)]

    def write_services(self, services: list[dict[str, object]]) -> None:
        write_json(
            self.processes_path, {"version": 1, "services": services, "updated_at": utc_now()}
        )

    def ports(self) -> dict[str, int]:
        value = read_json(self.ports_path, {})
        result = dict(DEFAULT_PORTS)
        if isinstance(value, dict):
            for role in result:
                with suppress(KeyError, TypeError, ValueError):
                    result[role] = int(value[role])
        return result

    def write_ports(self, ports: dict[str, int]) -> None:
        write_json(self.ports_path, ports)

    def installation(self) -> dict[str, object]:
        missing: list[str] = []
        if not self.env_path.is_file():
            missing.append(".env")
        if not self.paths_path.is_file():
            missing.append("config/native-paths.json")
        paths = read_json(self.paths_path, {})
        python_exe = Path(str(paths.get("python_exe", ""))) if isinstance(paths, dict) else Path()
        if not python_exe.is_file():
            missing.append("venv")
        if not (self.root / "web" / "index.html").is_file():
            missing.append("web/index.html")
        if not self.ports_path.is_file():
            missing.append("config/native-ports.json")
        return {
            "ready": not missing,
            "status": "READY" if not missing else "NOT_INSTALLED",
            "missing": missing,
        }

    def status(self, fast: bool) -> dict[str, object]:
        services = self.state_services()
        rows: list[dict[str, object]] = []
        seen: set[int] = set()
        total = 0
        for service in services:
            pid = service.get("pid", 0)
            healthy = process_alive(pid)
            process_id = int(pid) if str(pid).isdigit() else 0
            rss = process_rss(process_id) if process_id and process_id not in seen else 0
            if process_id:
                seen.add(process_id)
            total += rss
            rows.append(
                {
                    "service": service.get("name", ""),
                    "role": service.get("role", ""),
                    "pid": process_id,
                    "healthy": healthy,
                    "working_set_bytes": rss,
                    "working_set_mib": round(rss / 1024 / 1024, 1),
                    "embedded_in": service.get("embedded_in"),
                }
            )
        managed = [row for row in rows if not row.get("embedded_in")]
        runtime_healthy = bool(managed) and all(bool(row["healthy"]) for row in managed)
        if runtime_healthy and not fast:
            runtime_healthy = self.http_healthy(self.ports()["api"])
        watchdog = read_json(self.watchdog_path, None)
        return {
            "collected_at": utc_now(),
            "scope": "LINUX_NATIVE_PROCESS_GROUP",
            "total_working_set_bytes": total,
            "total_working_set_mib": round(total / 1024 / 1024, 1),
            "services": rows,
            "desired_state": self.desired_state(),
            "runtime_healthy": runtime_healthy,
            "ports": self.ports(),
            "watchdog_task": {
                "registered": False,
                "state": "Unavailable",
                "task_name": "ashare-ai-native-watchdog",
            },
            "watchdog": watchdog if isinstance(watchdog, dict) else None,
            "installation": self.installation(),
        }

    @staticmethod
    def http_healthy(port: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/v1/health", timeout=1
            ) as response:
                return response.status == 200
        except (OSError, ValueError):
            return False

    def read_env(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.env_path.is_file():
            for line in self.env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    values[key] = value
        return values

    def install(self) -> None:
        self.initialize()
        python = find_binary("python3.12", "python3.11", "python3") or sys.executable
        if not python:
            raise NativeError("Python 3.11 or 3.12 is required")
        venv = self.root / "venv"
        venv_python = venv / "bin" / "python"
        if not venv_python.is_file():
            subprocess.run([python, "-m", "venv", str(venv)], check=True)
        requirements = self.source_root / "requirements.runtime.lock"
        if requirements.is_file() and os.environ.get("ASHARE_NATIVE_SKIP_PIP") != "1":
            subprocess.run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    str(requirements),
                ],
                check=True,
            )
        postgres = os.environ.get("ASHARE_NATIVE_POSTGRES_BIN") or find_binary("pg_ctl")
        redis = os.environ.get("ASHARE_NATIVE_REDIS_BIN") or find_binary(
            "redis-server", "valkey-server"
        )
        redis_cli = find_binary("redis-cli", "valkey-cli")
        searx_check = subprocess.run([str(venv_python), "-c", "import searx"], check=False)
        missing = []
        if not postgres:
            missing.append("pg_ctl")
        if not redis:
            missing.append("redis-server or valkey-server")
        if not redis_cli:
            missing.append("redis-cli or valkey-cli")
        if searx_check.returncode != 0:
            missing.append("Python package searx")
        web_dist = self.source_root / "web" / "dist"
        if (web_dist / "index.html").is_file():
            if (self.root / "web").exists():
                shutil.rmtree(self.root / "web")
            shutil.copytree(web_dist, self.root / "web")
        else:
            missing.append("web/dist/index.html")
        import secrets

        env = self.read_env()
        postgres_password = env.get("POSTGRES_PASSWORD", secrets.token_urlsafe(24))
        redis_password = env.get("REDIS_PASSWORD", secrets.token_urlsafe(24))
        env.update(
            {
                "DATABASE_URL": f"postgresql+psycopg://ashare:{postgres_password}@127.0.0.1:{self.ports()['postgres']}/ashare",
                "REDIS_URL": f"redis://:{redis_password}@127.0.0.1:{self.ports()['redis']}/0",
                "SEARXNG_BASE_URL": f"http://127.0.0.1:{self.ports()['searxng']}/",
                "POSTGRES_PASSWORD": postgres_password,
                "REDIS_PASSWORD": redis_password,
                "ASHARE_NATIVE_WEB_ROOT": str(self.root / "web"),
                "EDGE_GATEWAY_SOURCE_DIR": str(self.config / "edge-gateway"),
                "EDGE_GATEWAY_HOST_SOURCE_DIR": str(self.config / "edge-gateway"),
                "EDGE_GATEWAY_CONFIG_DIR": str(self.config / "edge-gateway"),
                "EDGE_GATEWAY_LOG_DIR": str(self.root / "logs" / "edge-gateway"),
            }
        )
        atomic_write(
            self.env_path, "\n".join(f"{key}={value}" for key, value in sorted(env.items())) + "\n"
        )
        self.write_ports(self.ports())
        write_json(
            self.paths_path,
            {
                "python_exe": str(venv_python),
                "postgres_bin": str(Path(postgres).parent) if postgres else "",
                "redis_bin": str(Path(redis).parent) if redis else "",
                "redis_cli": redis_cli or "",
                "source_root": str(self.source_root),
                "version": "2026.08.03.2",
            },
        )
        if missing:
            raise NativeError(
                "Linux native installation is incomplete; missing: " + ", ".join(missing)
            )
        print(f"Linux native runtime prepared at {self.root}")

    def select_ports(self) -> dict[str, int]:
        current = self.ports()
        selected: dict[str, int] = {}
        for role in DEFAULT_PORTS:
            candidates = (current[role], *PORT_CANDIDATES[role])
            for candidate in candidates:
                if candidate not in selected.values() and port_available(candidate):
                    selected[role] = candidate
                    break
            else:
                raise NativeError(f"no free native port found for {role}")
        if selected != current:
            self.write_ports(selected)
            if self.env_path.is_file():
                updated: list[str] = []
                for line in self.env_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("DATABASE_URL="):
                        line = re.sub(r"(127\.0\.0\.1:)\d+", rf"\g<1>{selected['postgres']}", line)
                    elif line.startswith("REDIS_URL="):
                        line = re.sub(r"(127\.0\.0\.1:)\d+", rf"\g<1>{selected['redis']}", line)
                    elif line.startswith("SEARXNG_BASE_URL="):
                        line = re.sub(r"(127\.0\.0\.1:)\d+", rf"\g<1>{selected['searxng']}", line)
                    updated.append(line)
                atomic_write(self.env_path, "\n".join(updated) + "\n")
        return selected

    def start_process(
        self,
        name: str,
        command: list[str],
        environment: dict[str, str],
        services: list[dict[str, object]],
    ) -> None:
        output = (self.logs / f"{name}.out.log").open("ab")
        error = (self.logs / f"{name}.err.log").open("ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=self.source_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=error,
                start_new_session=True,
            )
        finally:
            output.close()
            error.close()
        services.append({"name": name, "role": name, "pid": process.pid, "started_at": utc_now()})

    def start(self) -> None:
        self.initialize()
        if not bool(self.installation()["ready"]):
            raise NativeError("native runtime is not installed; run install first")
        if any(process_alive(item.get("pid")) for item in self.state_services()):
            raise NativeError("native runtime is already running; stop it before starting again")
        paths = read_json(self.paths_path, {})
        if not isinstance(paths, dict):
            raise NativeError("native paths are invalid; run repair or install first")
        ports = self.select_ports()
        python = str(paths["python_exe"])
        postgres = str(Path(str(paths.get("postgres_bin", ""))) / "pg_ctl")
        redis = str(
            Path(str(paths.get("redis_bin", "")))
            / (
                "redis-server"
                if Path(str(paths.get("redis_bin", "")), "redis-server").exists()
                else "valkey-server"
            )
        )
        if not Path(postgres).is_file() or not Path(redis).is_file():
            raise NativeError("native PostgreSQL or Redis binary is missing; run install first")
        environment = os.environ.copy()
        environment.update(self.read_env())
        environment["PYTHONPATH"] = (
            str(self.source_root / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        )
        environment["ASHARE_NATIVE_WEB_ROOT"] = str(self.root / "web")
        environment["EDGE_GATEWAY_SOURCE_DIR"] = str(self.config / "edge-gateway")
        environment["EDGE_GATEWAY_HOST_SOURCE_DIR"] = str(self.config / "edge-gateway")
        environment["EDGE_GATEWAY_CONFIG_DIR"] = str(self.config / "edge-gateway")
        environment["EDGE_GATEWAY_LOG_DIR"] = str(self.root / "logs" / "edge-gateway")
        (self.config / "edge-gateway").mkdir(parents=True, exist_ok=True)
        (self.root / "logs" / "edge-gateway").mkdir(parents=True, exist_ok=True)
        services: list[dict[str, object]] = []
        self.set_desired_state("RUNNING")
        try:
            pg_data = self.root / "data" / "postgres"
            if not (pg_data / "PG_VERSION").is_file():
                initdb = str(Path(postgres).parent / "initdb")
                subprocess.run(
                    [initdb, "-D", str(pg_data), "-U", "ashare", "--auth=trust"], check=True
                )
            subprocess.run(
                [
                    postgres,
                    "-D",
                    str(pg_data),
                    "-o",
                    f"-p {ports['postgres']} -h 127.0.0.1",
                    "-w",
                    "start",
                ],
                check=True,
            )
            postgres_pid = int(
                (pg_data / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0]
            )
            services.append(
                {
                    "name": "postgres",
                    "role": "postgres",
                    "pid": postgres_pid,
                    "started_at": utc_now(),
                }
            )
            redis_config = self.config / "redis.conf"
            redis_lines = [
                "bind 127.0.0.1",
                f"port {ports['redis']}",
                f"dir {self.root / 'data' / 'redis'}",
                "appendonly yes",
            ]
            if environment.get("REDIS_PASSWORD"):
                redis_lines.append(f"requirepass {environment['REDIS_PASSWORD']}")
            atomic_write(redis_config, "\n".join(redis_lines) + "\n")
            redis_args = [redis, str(redis_config)]
            self.start_process("redis", redis_args, environment, services)
            subprocess.run(
                [python, "-m", "ashare_ai.cli", "migrate"],
                cwd=self.source_root,
                env=environment,
                check=True,
            )
            self.start_process(
                "searxng",
                [python, "-m", "searx.webapp", "--port", str(ports["searxng"])],
                environment,
                services,
            )
            self.start_process(
                "api",
                [
                    python,
                    "-m",
                    "ashare_ai.cli",
                    "api",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(ports["api"]),
                ],
                environment,
                services,
            )
            services.append(
                {
                    "name": "web",
                    "role": "web",
                    "pid": services[-1]["pid"],
                    "embedded_in": "api",
                    "started_at": utc_now(),
                }
            )
            self.start_process(
                "job-worker",
                [python, "-m", "ashare_ai.orchestration.serial_worker"],
                environment,
                services,
            )
            self.start_process(
                "exit-advice-worker",
                [python, "-m", "ashare_ai.orchestration.exit_advice_worker"],
                environment,
                services,
            )
            self.write_services(services)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not self.http_healthy(ports["api"]):
                time.sleep(1)
            if not self.http_healthy(ports["api"]):
                raise NativeError(f"native API did not become ready on port {ports['api']}")
            print(f"Linux native runtime started at http://127.0.0.1:{ports['api']}/")
        except Exception:
            self.stop_processes(services)
            self.set_desired_state("STOPPED")
            self.write_services([])
            raise

    def stop_processes(self, services: list[dict[str, object]]) -> None:
        for service in reversed(services):
            if service.get("embedded_in"):
                continue
            with suppress(OSError, TypeError, ValueError):
                os.kill(int(service.get("pid", 0)), signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            process_alive(item.get("pid")) for item in services
        ):
            time.sleep(0.2)
        for service in services:
            try:
                if process_alive(service.get("pid")):
                    os.kill(int(service["pid"]), signal.SIGKILL)
            except (OSError, TypeError, ValueError):
                pass

    def stop(self) -> None:
        self.initialize()
        self.set_desired_state("STOPPED")
        services = self.state_services()
        self.stop_processes(services)
        paths = read_json(self.paths_path, {})
        if isinstance(paths, dict):
            pg_data = self.root / "data" / "postgres"
            pg_ctl = Path(str(paths.get("postgres_bin", ""))) / "pg_ctl"
            if pg_ctl.is_file() and (pg_data / "postmaster.pid").is_file():
                subprocess.run(
                    [str(pg_ctl), "-D", str(pg_data), "-m", "fast", "-w", "stop"], check=False
                )
        self.write_services([])
        print("Linux native runtime stopped")

    def repair(self) -> None:
        self.initialize()
        self.write_ports(self.select_ports())
        print(f"Linux native runtime configuration repaired at {self.root}")

    def doctor(self, as_json: bool) -> int:
        self.initialize()
        installation = self.installation()
        checks = [
            {"check": "runtime-root-outside-source", "status": "PASS", "detail": str(self.root)},
            {
                "check": "native-installation",
                "status": "PASS" if installation["ready"] else "FAIL",
                "detail": installation,
            },
            {
                "check": "python",
                "status": "PASS" if find_binary("python3") else "FAIL",
                "detail": "python3",
            },
            {
                "check": "postgres",
                "status": "PASS" if find_binary("pg_ctl") else "WARN",
                "detail": "pg_ctl",
            },
            {
                "check": "redis",
                "status": "PASS" if find_binary("redis-server", "valkey-server") else "WARN",
                "detail": "redis-compatible",
            },
            {
                "check": "docker-processes",
                "status": "PASS",
                "detail": "linux native controller does not start Docker",
            },
        ]
        if as_json:
            print(json.dumps(checks, ensure_ascii=False, indent=2))
        else:
            for check in checks:
                print(f"{check['check']}: {check['status']} ({check['detail']})")
        return 1 if any(check["status"] == "FAIL" for check in checks) else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source_root = find_source_root(Path.cwd())
    if not (source_root / "pyproject.toml").is_file():
        source_root = find_source_root(Path(__file__).resolve().parent)
    default_root = Path(__file__).resolve().parent / "runtime"
    if is_inside(default_root, source_root):
        default_root = Path.home() / ".local" / "share" / "ashare-ai" / "runtime"
    configured_root = Path(__file__).resolve().with_name("runtime-root.txt")
    if configured_root.is_file():
        with suppress(OSError):
            configured_value = configured_root.read_text(encoding="utf-8").strip()
            if configured_value:
                default_root = Path(configured_value).expanduser()
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--root", default=str(default_root))
    parser.add_argument("--source-root", default=str(source_root))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--research-mode", choices=("SERIAL", "DUAL"), default="SERIAL")
    parser.add_argument("--research-workers", type=int, default=0)
    parser.add_argument("--watchdog-interval", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    controller = Controller(
        Path(args.root),
        Path(args.source_root),
        args.research_mode,
        args.research_workers,
        args.watchdog_interval,
    )
    try:
        if args.command == "status":
            controller.initialize()
            report = controller.status(args.fast)
            if args.json:
                print(
                    json.dumps(
                        report, ensure_ascii=False, separators=(",", ":") if args.fast else None
                    )
                )
            else:
                print("Linux native runtime is " + report["desired_state"])
            return 0
        if args.command == "doctor":
            return controller.doctor(args.json)
        with management_lock(controller.lock_path):
            if args.command == "install":
                controller.install()
            elif args.command == "start":
                controller.start()
            elif args.command == "stop":
                controller.stop()
            elif args.command == "restart":
                controller.stop()
                controller.start()
            elif args.command == "repair":
                controller.repair()
        return 0
    except (NativeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Linux native command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
