#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import webbrowser
from pathlib import Path
from tkinter import BooleanVar, IntVar, StringVar, Tk, filedialog, messagebox, ttk

DEFAULT_CONTROLLER = Path(__file__).with_name("ashare-native-linux.sh")
COMMANDS = {"install", "start", "stop", "restart", "repair", "doctor", "status"}


def find_source_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        packaged_app = candidate / "app"
        if (packaged_app / "pyproject.toml").is_file() and (packaged_app / "src").is_dir():
            return packaged_app
        if (candidate / "pyproject.toml").is_file() and (candidate / "src").is_dir():
            return candidate
    return current


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def default_source_root() -> Path:
    current = find_source_root(Path.cwd())
    if (current / "pyproject.toml").is_file() and (current / "src").is_dir():
        return current
    return find_source_root(Path(__file__).resolve().parent)


def default_runtime_root(source_root: Path) -> Path:
    manager_directory = Path(__file__).resolve().parent
    candidate = manager_directory / "runtime"
    if is_inside(candidate, source_root):
        return Path.home() / ".local" / "share" / "ashare-ai" / "runtime"
    return candidate


class ControlCenter:
    def __init__(self, root: Tk, controller: Path, source_root: Path, runtime_root: Path) -> None:
        self.root = root
        self.controller = controller
        self.source_root = source_root
        self.runtime_root = StringVar(value=str(runtime_root))
        self.research_mode = StringVar(value="SERIAL")
        self.research_workers = IntVar(value=0)
        self.watchdog_interval = IntVar(value=10)
        self.auto_refresh = BooleanVar(value=True)
        self.status_busy = False
        self.action_busy = False
        self.status_pending = False
        self.queued_action: tuple[str, bool] | None = None
        self.action_buttons: list[ttk.Button] = []
        self.last_report: dict[str, object] | None = None

        root.title("AshareAI Linux Native Control Center")
        root.geometry("1040x720")
        root.minsize(940, 640)
        self._build()
        self.refresh_status()
        self._schedule_refresh()

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Header.TFrame", background="#ffffff")
        style.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure(
            "Title.TLabel",
            background="#ffffff",
            foreground="#111827",
            font=("Segoe UI", 16, "bold"),
        )
        style.configure("Muted.TLabel", background="#ffffff", foreground="#6b7280")
        style.configure("Primary.TButton", foreground="#ffffff", background="#1b6f5b")

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(20, 12))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="AshareAI Linux 本机运行管理器",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="非 Docker 安装、启动、诊断和状态查看",
            style="Muted.TLabel",
        ).pack(anchor="w")

        config = ttk.Frame(self.root, style="Card.TFrame", padding=12)
        config.pack(fill="x", padx=14, pady=(14, 8))
        config.columnconfigure(1, weight=1)
        ttk.Label(config, text="运行目录").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(config, textvariable=self.runtime_root).grid(row=0, column=1, sticky="ew")
        ttk.Button(config, text="浏览", command=self.choose_runtime).grid(
            row=0,
            column=2,
            padx=(8, 0),
        )
        ttk.Button(config, text="打开", command=self.open_runtime).grid(
            row=0,
            column=3,
            padx=(8, 0),
        )
        ttk.Label(config, text="模式").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(
            config,
            textvariable=self.research_mode,
            values=("SERIAL", "DUAL"),
            width=10,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", pady=(12, 0))
        ttk.Label(config, text="研究进程").grid(
            row=1,
            column=1,
            sticky="w",
            padx=(130, 0),
            pady=(12, 0),
        )
        ttk.Spinbox(
            config,
            textvariable=self.research_workers,
            from_=0,
            to=2,
            width=5,
        ).grid(row=1, column=1, sticky="w", padx=(200, 0), pady=(12, 0))
        ttk.Label(config, text="看门狗秒数").grid(
            row=1,
            column=1,
            sticky="w",
            padx=(280, 0),
            pady=(12, 0),
        )
        ttk.Spinbox(
            config,
            textvariable=self.watchdog_interval,
            from_=5,
            to=300,
            width=6,
        ).grid(row=1, column=1, sticky="w", padx=(380, 0), pady=(12, 0))
        ttk.Checkbutton(config, text="自动刷新", variable=self.auto_refresh).grid(
            row=1,
            column=1,
            sticky="w",
            padx=(470, 0),
            pady=(12, 0),
        )

        actions = ttk.Frame(self.root, style="Card.TFrame", padding=10)
        actions.pack(fill="x", padx=14, pady=8)
        for command, label in [
            ("start", "启动"),
            ("stop", "停止"),
            ("restart", "重启"),
            ("repair", "修复"),
            ("install", "安装更新"),
            ("doctor", "诊断"),
        ]:
            button = ttk.Button(
                actions,
                text=label,
                command=lambda value=command: self.run_command(value),
            )
            button.pack(side="left", padx=(0, 8))
            self.action_buttons.append(button)
        self.open_web_button = ttk.Button(
            actions, text="打开 Web", command=self.open_web, state="disabled"
        )
        self.open_web_button.pack(side="right", padx=(8, 0))
        self.refresh_button = ttk.Button(actions, text="刷新", command=self.refresh_status)
        self.refresh_button.pack(side="right")

        summary = ttk.Frame(self.root, style="Card.TFrame", padding=14)
        summary.pack(fill="x", padx=14, pady=8)
        self.state = ttk.Label(summary, text="正在检查...", font=("Segoe UI", 15, "bold"))
        self.state.pack(side="left", padx=(0, 38))
        self.health = ttk.Label(summary, text="健康状态：--")
        self.health.pack(side="left", padx=(0, 38))
        self.memory = ttk.Label(summary, text="内存：--")
        self.memory.pack(side="left", padx=(0, 38))
        self.watchdog = ttk.Label(summary, text="看门狗：--")
        self.watchdog.pack(side="left", padx=(0, 38))
        self.ports = ttk.Label(summary, text="端口：--")
        self.ports.pack(side="left")

        tabs = ttk.Notebook(self.root)
        tabs.pack(fill="both", expand=True, padx=14, pady=(8, 8))
        services_page = ttk.Frame(tabs)
        tabs.add(services_page, text="服务")
        columns = ("service", "role", "pid", "healthy", "memory", "embedded")
        self.services = ttk.Treeview(services_page, columns=columns, show="headings")
        labels = ("服务", "角色", "PID", "健康", "内存 MiB", "内嵌于")
        for column, label in zip(columns, labels, strict=True):
            self.services.heading(column, text=label)
            self.services.column(column, width=130)
        self.services.pack(fill="both", expand=True)

        activity_page = ttk.Frame(tabs)
        tabs.add(activity_page, text="活动记录")
        self.activity = self._text(activity_page)
        log_page = ttk.Frame(tabs)
        tabs.add(log_page, text="看门狗日志")
        self.watchdog_log = self._text(log_page)
        tabs.bind("<<NotebookTabChanged>>", lambda _event: self.load_watchdog_log())

        self.footer = ttk.Label(self.root, text="就绪")
        self.footer.pack(fill="x", padx=16, pady=(0, 8))

    def _text(self, parent: ttk.Frame):
        import tkinter as tk

        box = tk.Text(parent, wrap="none", font=("Consolas", 10))
        box.pack(fill="both", expand=True)
        return box

    def choose_runtime(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.runtime_root.get())
        if selected:
            self.runtime_root.set(selected)
            self.persist_runtime_root()
            self.refresh_status()

    def persist_runtime_root(self) -> None:
        try:
            config_path = self.controller.resolve().with_name("runtime-root.txt")
            runtime = Path(self.runtime_root.get()).expanduser().resolve()
            config_path.write_text(str(runtime) + "\n", encoding="utf-8")
        except OSError as exc:
            self.append_activity(f"保存运行目录失败：{exc}")

    def open_runtime(self) -> None:
        path = Path(self.runtime_root.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        self.persist_runtime_root()
        subprocess.Popen(["xdg-open", str(path)])

    def open_web(self) -> None:
        if not self.last_report:
            return
        ports = self.last_report.get("ports")
        if isinstance(ports, dict) and ports.get("api"):
            webbrowser.open(f"http://127.0.0.1:{ports['api']}/")

    def refresh_status(self) -> None:
        self.run_command("status", as_json=True)

    def run_command(self, command: str, as_json: bool = False) -> None:
        if command not in COMMANDS:
            messagebox.showerror("AshareAI", f"未知命令：{command}")
            return
        self.persist_runtime_root()
        if command == "status":
            if self.status_busy:
                return
            if self.action_busy:
                self.status_pending = True
                return
            self.status_busy = True
        else:
            if self.action_busy:
                return
            if self.status_busy:
                if self.queued_action is None:
                    self.queued_action = (command, as_json)
                    self.append_activity(f"状态刷新完成后将执行：{command}")
                return
            self.action_busy = True
            self._set_action_buttons(False)
        self.footer.configure(text=f"正在执行 {command}...")
        thread = threading.Thread(target=self._run_worker, args=(command, as_json), daemon=True)
        thread.start()

    def _run_worker(self, command: str, as_json: bool) -> None:
        try:
            if not self.controller.is_file():
                raise FileNotFoundError(f"Linux 控制器尚未就绪：{self.controller}")
            args = [
                str(self.controller),
                command,
                "--root",
                self.runtime_root.get(),
                "--source-root",
                str(self.source_root),
            ]
            if command in {"install", "start", "restart"}:
                args += [
                    "--research-mode",
                    self.research_mode.get(),
                    "--research-workers",
                    str(self.research_workers.get()),
                    "--watchdog-interval",
                    str(self.watchdog_interval.get()),
                ]
            if as_json:
                args.append("--json")
            result = subprocess.run(args, text=True, capture_output=True, check=False)
            self.root.after(
                0,
                self._complete,
                command,
                result.returncode,
                result.stdout,
                result.stderr,
                as_json,
            )
        except Exception as exc:
            self.root.after(0, self._complete, command, 1, "", str(exc), as_json)

    def _complete(self, command: str, code: int, stdout: str, stderr: str, as_json: bool) -> None:
        if command == "status":
            self.status_busy = False
        else:
            self.action_busy = False
            self._set_action_buttons(True)
        self.footer.configure(text=f"{command} 完成，退出码 {code}")
        parsed = False
        if as_json and stdout.strip():
            try:
                self.update_report(json.loads(stdout))
                parsed = True
            except json.JSONDecodeError:
                pass
        if not parsed and stdout.strip():
            self.append_activity(stdout.strip())
        if not parsed and stderr.strip():
            self.append_activity(stderr.strip())
        if command != "status":
            self.status_pending = False
            self.refresh_status()
        elif self.queued_action is not None:
            queued_command, queued_json = self.queued_action
            self.queued_action = None
            self.run_command(queued_command, queued_json)
        elif self.status_pending:
            self.status_pending = False
            self.refresh_status()

    def update_report(self, report: dict[str, object]) -> None:
        self.last_report = report
        desired = str(report.get("desired_state", "STOPPED"))
        self.state.configure(
            text={"RUNNING": "运行中", "STOPPED": "已停止"}.get(desired, desired),
        )
        health_text = "健康" if report.get("runtime_healthy") else "未就绪"
        self.health.configure(text=f"健康状态：{health_text}")
        self.memory.configure(text=f"内存：{report.get('total_working_set_mib', 0)} MiB")
        watchdog = report.get("watchdog")
        watchdog_status = watchdog.get("status") if isinstance(watchdog, dict) else "MISSING"
        self.watchdog.configure(text=f"看门狗：{watchdog_status}")
        ports = report.get("ports")
        if isinstance(ports, dict):
            port_text = (
                f"端口：PG {ports.get('postgres', '--')} "
                f"Redis {ports.get('redis', '--')} API {ports.get('api', '--')}"
            )
            self.ports.configure(text=port_text)
            self.open_web_button.configure(
                state="normal"
                if report.get("runtime_healthy") and ports.get("api")
                else "disabled",
            )
        else:
            self.open_web_button.configure(state="disabled")
        for item in self.services.get_children():
            self.services.delete(item)
        services = report.get("services", [])
        if not isinstance(services, list):
            services = []
        for service in services:
            if isinstance(service, dict):
                self.services.insert(
                    "",
                    "end",
                    values=(
                        service.get("service", ""),
                        service.get("role", ""),
                        service.get("pid", ""),
                        "是" if service.get("healthy") else "否",
                        service.get("working_set_mib", 0),
                        service.get("embedded_in", ""),
                    ),
                )

    def append_activity(self, text: str) -> None:
        self.activity.insert("end", text + "\n")
        self.activity.see("end")

    def load_watchdog_log(self) -> None:
        log_path = Path(self.runtime_root.get()).expanduser() / "logs" / "watchdog.log"
        if not log_path.is_file():
            return
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        self.watchdog_log.delete("1.0", "end")
        self.watchdog_log.insert("end", "\n".join(lines))

    def _set_action_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.action_buttons:
            button.configure(state=state)

    def _schedule_refresh(self) -> None:
        if self.auto_refresh.get() and not self.status_busy:
            self.refresh_status()
        self.root.after(10000, self._schedule_refresh)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source_root = default_source_root()
    default_root = default_runtime_root(source_root)
    configured_root = DEFAULT_CONTROLLER.with_name("runtime-root.txt")
    if configured_root.is_file():
        try:
            configured_value = configured_root.read_text(encoding="utf-8").strip()
            if configured_value:
                default_root = Path(configured_value).expanduser()
        except OSError:
            pass
    parser.add_argument(
        "--controller",
        default=os.environ.get("ASHARE_NATIVE_LINUX_CONTROLLER", str(DEFAULT_CONTROLLER)),
    )
    parser.add_argument("--source-root", default=str(source_root))
    parser.add_argument("--root", default=str(default_root))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Tk()
    ControlCenter(root, Path(args.controller), Path(args.source_root), Path(args.root))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
