#!/usr/bin/env python3
"""Cross-platform, host-side synchronizer for optional Compose profiles."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import cast


def _log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


class Controller:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.secrets = root / ".secrets"
        self.log_path = self.secrets / "topology-controller.log"
        self.state_path = self.secrets / "topology-controller.state"
        self.edge_state_path = self.secrets / "topology-controller.edge.state"

    def _docker(
        self, *args: str, env_extra: dict[str, str] | None = None
    ) -> str:
        env = {**os.environ, **env_extra} if env_extra else None
        result = subprocess.run(
            ["docker", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
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
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise RuntimeError("topology controller response is not an object")
        return cast(dict[str, object], payload)

    def _validate_edge(self, values: dict[str, str]) -> None:
        if not values.get("EDGE_DOMAIN", "").strip():
            raise RuntimeError("edge-gateway requires a public domain")
        if not values.get("EDGE_ACME_EMAIL", "").strip():
            raise RuntimeError("edge-gateway requires an ACME account email")
        if values.get("EDGE_FRPC_ENABLED") == "true":
            config_path = values.get("EDGE_FRPC_CONFIG_FILE", "").strip()
            if not config_path:
                raise RuntimeError("edge-gateway frpc requires a config file path")
            path = Path(config_path)
            if not path.is_absolute():
                path = self.root / path
            if not path.is_file():
                raise RuntimeError("edge-gateway frpc config file is missing")

    def _edge_values(self, desired: dict[str, object]) -> dict[str, str]:
        return {
            "EDGE_DOMAIN": str(desired.get("edge_domain") or ""),
            "EDGE_ACME_EMAIL": str(desired.get("edge_acme_email") or ""),
            "EDGE_ACME_CA_SERVER": str(
                desired.get("edge_acme_ca_server") or "letsencrypt"
            ),
            "EDGE_FRPC_ENABLED": "true" if bool(desired.get("edge_frpc_enabled")) else "false",
            "EDGE_FRPC_CONFIG_FILE": str(
                desired.get("edge_frpc_config_file")
                or "./docker/edge-gateway/frpc.disabled.toml"
            ),
        }

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
        # Edge config is administrator-overridable from the system settings
        # center, so the desired values come from the API (not .env).  The
        # digest lets a domain/ACME/frpc change recreate the gateway while the
        # enabled-flag alone only starts or stops it.
        edge_values = self._edge_values(desired)
        edge_digest = hashlib.sha256(
            json.dumps(edge_values, sort_keys=True).encode("utf-8")
        ).hexdigest()
        edge_state = f"{1 if edge else 0}:{edge_digest}"
        try:
            if self._read_state(self.edge_state_path) != edge_state:
                if edge:
                    self._validate_edge(edge_values)
                    self._docker(
                        *base,
                        "--profile",
                        "edge",
                        "up",
                        "-d",
                        "--no-build",
                        "--force-recreate",
                        "edge-gateway",
                        env_extra=edge_values,
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
