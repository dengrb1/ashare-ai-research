from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tools.console.config_store import PAGE_MAPPINGS
import tools.console.stack_controller as stack_controller
from tools.console.stack_controller import StackController


def test_dev_workbench_is_registered() -> None:
    assert PAGE_MAPPINGS["dev"]["title"] == "开发工作台"


def test_native_start_uses_source_and_dev_mode(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "checkout"
    (source_root / "scripts" / "native").mkdir(parents=True)
    script = source_root / "scripts" / "native" / "ashare-native.ps1"
    script.touch()
    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    controller = StackController(source_root, lambda *_: None)

    def capture(label: str, actual_source: Path, args: list[str]) -> bool:
        captured.update(label=label, source=actual_source, args=args)
        return True

    monkeypatch.setattr(controller, "_schedule_external_script", capture)

    assert controller.native_start(source_root, runtime_root)
    assert captured["source"] == source_root.resolve()
    assert captured["args"] == [
        "-Command",
        "start",
        "-SourceRoot",
        str(source_root.resolve()),
        "-Root",
        str(runtime_root.resolve()),
        "-DevMode",
    ]


def test_web_build_command_runs_through_windows_command_processor(monkeypatch) -> None:
    monkeypatch.setattr(stack_controller, "os", SimpleNamespace(name="nt"))

    command = StackController._npm_command("run", "build")

    assert command[:4] == ["cmd.exe", "/d", "/s", "/c"]
    assert command[4] == 'npm.cmd run build'


def test_decode_log_bytes_removes_ansi_control_sequences() -> None:
    assert stack_controller.decode_log_bytes(b"\x1b[31;1mGateway failed\x1b[0m") == "Gateway failed"


def test_full_build_task_is_scheduled_from_gui_controller(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "scripts" / "dev.ps1"
    script.parent.mkdir()
    script.touch()
    captured: dict[str, object] = {}
    controller = StackController(tmp_path, lambda *_: None)

    monkeypatch.setattr(
        "tools.console.stack_controller.shell_command",
        lambda: ["powershell.exe", "-NoProfile"],
    )
    def capture(label, command, cwd, env=None) -> bool:
        captured.update(label=label, command=command, cwd=cwd)
        return True

    monkeypatch.setattr(controller, "_schedule_process", capture)

    assert controller.build_all(tmp_path)
    assert captured["label"] == "一键全量构建"
    assert captured["command"] == [
        "powershell.exe",
        "-NoProfile",
        "-File",
        str(script.resolve()),
        "-Task",
        "all",
    ]
    assert captured["cwd"] == tmp_path.resolve()
