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

    def run(self) -> int:
        try:
            desired = self._desired()
            mode = str(desired["research_execution_mode"])
            edge = bool(desired["edge_gateway_enabled"])
            state = f"{mode}:{edge}"
            if (
                self.state_path.is_file()
                and self.state_path.read_text(encoding="ascii").strip() == state
            ):
                return 0
            base = ("compose", "-p", "ashare-ai-src", "-f", "compose.yaml")
            if mode == "DUAL":
                self._docker(
                    *base,
                    "--profile",
                    "dual-research",
                    "up",
                    "-d",
                    "--no-build",
                    "job-worker",
                    "research-worker",
                )
            else:
                self._stop_if_running("dual-research", "research-worker")
                self._docker(*base, "up", "-d", "--no-build", "job-worker")
            if edge:
                self._validate_edge()
                self._docker(*base, "--profile", "edge", "up", "-d", "--no-build", "edge-gateway")
            else:
                self._stop_if_running("edge", "edge-gateway")
            self.state_path.write_text(state, encoding="ascii")
            return 0
        except Exception as exc:  # scheduler-friendly: errors stay in the private local log
            _log(self.log_path, f"ERROR {exc}")
            return 1


def main() -> int:
    return Controller(Path(__file__).resolve().parent.parent).run()


if __name__ == "__main__":
    sys.exit(main())
