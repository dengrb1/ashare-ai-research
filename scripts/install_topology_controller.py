#!/usr/bin/env python3
"""Install the cross-platform topology controller scheduler without Docker access."""

from __future__ import annotations

import argparse
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTROLLER = ROOT / "scripts" / "topology_controller.py"


def _command(*args: str) -> None:
    subprocess.run(args, check=True)


def _token_and_env() -> None:
    secret_dir = ROOT / ".secrets"
    secret_dir.mkdir(exist_ok=True)
    token_file = secret_dir / "topology-controller.token"
    token = (
        token_file.read_text(encoding="ascii").strip()
        if token_file.exists()
        else secrets.token_urlsafe(32)
    )
    token_file.write_text(token, encoding="ascii")
    env_file = ROOT / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [line for line in lines if not line.startswith("TOPOLOGY_CONTROLLER_TOKEN=")]
    lines.append(f"TOPOLOGY_CONTROLLER_TOKEN={token}")
    # Edge-gateway configuration is administrator-managed in the system settings
    # center (System Settings -> 高级配置 -> 公网边缘网关) and is injected by the
    # controller from /api/internal/topology-desired, so the installer no longer
    # seeds EDGE_* defaults into .env.
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _install_windows() -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    task = f'"{executable}" "{CONTROLLER}"'
    _command(
        "schtasks.exe",
        "/Create",
        "/TN",
        "AshareAiTopologyController",
        "/TR",
        task,
        "/SC",
        "MINUTE",
        "/MO",
        "1",
        "/F",
    )


def _install_linux() -> None:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = "[Service]\nType=oneshot\nExecStart=" + f"{sys.executable} {CONTROLLER}\n"
    timer = (
        "[Unit]\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1min\n[Install]\nWantedBy=timers.target\n"
    )
    (unit_dir / "ashare-ai-topology.service").write_text(service, encoding="utf-8")
    (unit_dir / "ashare-ai-topology.timer").write_text(timer, encoding="utf-8")
    _command("systemctl", "--user", "daemon-reload")
    _command("systemctl", "--user", "enable", "--now", "ashare-ai-topology.timer")


def _install_macos() -> None:
    path = Path.home() / "Library" / "LaunchAgents" / "ai.ashare.topology.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump(
            {
                "Label": "ai.ashare.topology",
                "ProgramArguments": [sys.executable, str(CONTROLLER)],
                "StartInterval": 60,
                "RunAtLoad": True,
                "StandardOutPath": str(ROOT / ".secrets" / "topology-controller.log"),
                "StandardErrorPath": str(ROOT / ".secrets" / "topology-controller.log"),
            },
            stream,
        )
    _command("launchctl", "bootstrap", f"gui/{os.getuid()}", str(path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if not args.install:
        parser.error("pass --install to register the platform scheduler")
    if shutil.which("docker") is None:
        parser.error("Docker CLI is required, but this installer never starts containers")
    _token_and_env()
    if sys.platform == "win32":
        _install_windows()
    elif sys.platform == "darwin":
        _install_macos()
    else:
        _install_linux()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
