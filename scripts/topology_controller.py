#!/usr/bin/env python3
"""Cross-platform, host-side synchronizer for optional Compose profiles."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path


def _log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


class Controller:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.secrets = root / ".secrets"
        self.log_path = self.secrets / "topology-controller.log"
        self.state_path = self.secrets / "topology-controller.state"
        self.edge_state_path = self.secrets / "topology-controller.edge.state"

    def _docker(self, *args: str) -> str:
        result = subprocess.run(
            ["docker", *args], cwd=self.root, text=True, capture_output=True, check=False
        )
        for output in (result.stdout, result.stderr):
            if output:
                for line in output.splitlines():
                    _log(self.log_path, line)
        if result.returncode:
            raise RuntimeError(f"docker compose failed with exit code {result.returncode}")
        return result.stdout

    def _stop_if_running(self, profile: str, service: str) -> None:
        base = ("compose", "-p", "ashare-ai-src", "-f", "compose.yaml")
        running = self._docker(
            *base, "--profile", profile, "ps", "--status", "running", "-q", service
        )
        if running.strip():
            self._docker(*base, "--profile", profile, "stop", service)

    def _read_state(self, path: Path) -> str:
        return path.read_text(encoding="ascii").strip() if path.is_file() else ""

    def _desired(self) -> dict[str, object]:
        token = (self.secrets / "topology-controller.token").read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeError("topology controller token is invalid")
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/internal/topology-desired",
            headers={"X-Topology-Controller-Token": token},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def _validate_edge(self) -> None:
        env_file = self.root / ".env"
        if not env_file.is_file():
            raise RuntimeError("edge-gateway requires a local .env file")
        values = _env(env_file)
        for name in ("EDGE_DOMAIN", "EDGE_ACME_EMAIL", "EDGE_FRPC_CONFIG_FILE"):
            if not values.get(name, "").strip():
                raise RuntimeError(f"edge-gateway requires {name} in .env")
        frpc = Path(values["EDGE_FRPC_CONFIG_FILE"])
        if not frpc.is_absolute():
            frpc = self.root / frpc
        if not frpc.is_file():
            raise RuntimeError("edge-gateway frpc config file is missing")

    def _apply_workers(self, base: tuple[str, ...], mode: str, force: bool) -> None:
        if mode == "DUAL":
            args = [*base, "--profile", "dual-research", "up", "-d", "--no-build"]
            if force:
                args.append("--force-recreate")
            self._docker(*args, "job-worker", "research-worker")
        else:
            self._stop_if_running("dual-research", "research-worker")
            args = [*base, "up", "-d", "--no-build"]
            if force:
                args.append("--force-recreate")
            self._docker(*args, "job-worker")

    def run(self) -> int:
        try:
            desired = self._desired()
        except Exception as exc:  # scheduler-friendly: errors stay in the private local log
            _log(self.log_path, f"ERROR {exc}")
            return 1
        mode = str(desired["research_execution_mode"])
        edge = bool(desired["edge_gateway_enabled"])
        auto_restart = bool(desired.get("auto_restart_enabled", False))
        restart_required = bool(desired.get("restart_required", False))
        topology_sha = str(desired.get("topology_sha256", ""))
        base = ("compose", "-p", "ashare-ai-src", "-f", "compose.yaml")
        result = 0
        force = auto_restart and restart_required
        worker_state = f"{mode}:{topology_sha}:{1 if force else 0}"
        try:
            if self._read_state(self.state_path) != worker_state:
                # Without auto-restart the synchronizer only starts/stops the
                # profile-gated services and the operator runs the command shown
                # in the UI.  With auto-restart, a pending topology transition
                # (a persisted change, or enabling auto-restart while a worker
                # is stale) recreates the affected workers so the boot-time
                # topology takes effect.  Once recorded as forced, a later
                # stale heartbeat or a manual `docker stop` never recreates
                # again.
                self._apply_workers(base, mode, force)
                self.state_path.write_text(worker_state, encoding="ascii")
        except Exception as exc:
            _log(self.log_path, f"ERROR {exc}")
            result = 1
        edge_state = "1" if edge else "0"
        try:
            if self._read_state(self.edge_state_path) != edge_state:
                if edge:
                    self._validate_edge()
                    self._docker(
                        *base, "--profile", "edge", "up", "-d", "--no-build", "edge-gateway"
                    )
                else:
                    self._stop_if_running("edge", "edge-gateway")
                self.edge_state_path.write_text(edge_state, encoding="ascii")
        except Exception as exc:
            # Edge enablement is best-effort and independent: a missing or
            # invalid frpc configuration must never block worker-topology
            # synchronisation or its state tracking.  It retries next poll.
            _log(self.log_path, f"ERROR edge {exc}")
            result = 1
        return result


def main() -> int:
    return Controller(Path(__file__).resolve().parent.parent).run()


if __name__ == "__main__":
    sys.exit(main())
