"""Dark gateway monitor and configuration center for the desktop EXE.

The executable remains a standard-library Tkinter presentation layer. Process
lifecycle work and non-blocking probes live in :mod:`stack_controller`; local
file formats and atomic saves live in :mod:`config_store`.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Mapping

from config_store import (
    ConfigError,
    ConfigStore,
    DEFAULT_CONTROL_API_PORT,
    PAGE_MAPPINGS,
    SELECT_OPTIONS,
    add_gateway_channel,
    infer_field_type,
    mask_secret,
    preserve_secret,
)
from stack_controller import (
    HealthSnapshot,
    LIFECYCLE_SCRIPT,
    OperationResult,
    StackController,
    decode_log_bytes,
    find_repo_root,
    shell_command,
    tail_file,
)

APP_TITLE = "A-Share AI Trader · Gateway Console"
CONFIG_FILE = Path.home() / ".ashare_trader_console.json"
STATUS_POLL_SECONDS = 3.0
LOG_VIEWER_REFRESH_MS = 3000
LOG_VIEWER_MAX_BYTES = 1024 * 1024
CONSOLE_LOG_MAX_BYTES = 1024 * 1024
CONSOLE_LOG_BACKUPS = 3
PUMP_MAX_PER_TICK = 200
_CONSOLE_LOGGER_NAME = "trader_console"

COLORS = {
    "bg": "#09111f",
    "sidebar": "#0d1728",
    "surface": "#111e31",
    "surface_2": "#172943",
    "surface_3": "#1b3150",
    "line": "#263b59",
    "text": "#eef5ff",
    "muted": "#91a5c0",
    "faint": "#627795",
    "cyan": "#5ed8ff",
    "green": "#52dfa0",
    "amber": "#f5c266",
    "red": "#ff7187",
    "purple": "#a891ff",
    "blue": "#6eb8ff",
}

UI_FONT = "Segoe UI"
MONO_FONT = "Consolas"
COMPACT_WIDTH = 1220

NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("gateway", "⌂", "网关监控"),
    ("agent", "◈", "Agent 配置"),
    ("channels", "◇", "渠道中心"),
    ("subscriptions", "◎", "订阅中心"),
    ("environment", "⚙", "环境参数"),
    ("trading", "◌", "交易与风控"),
    ("strategy", "▥", "策略与数据"),
    ("admin", "▣", "管理面板"),
    ("cockpit", "◉", "驾驶舱"),
)

PAGE_SUBTITLES = {
    "gateway": "实时观察网关、服务生命周期与运行日志",
    "agent": "Agent 画像、模型供应商和任务路由",
    "channels": "编辑 Gateway 标量配置与渠道健康策略",
    "subscriptions": "管理默认模型、支持模型与模型映射",
    "environment": "运行端口、日志位置和行情数据目录",
    "trading": "交易账户、执行参数与风险阈值",
    "strategy": "策略信号、行情源与数据刷新参数",
    "admin": "配置重读、日志来源和本地路径",
    "cockpit": "打开现有 Web 控制台查看交易驾驶舱",
}


class TraderConsole:
    """Tk presentation layer for :class:`StackController`."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1320x860")
        self.root.minsize(1100, 700)
        self.root.configure(bg=COLORS["bg"])

        self.repo_root: Path | None = self._load_repo_root()
        self.config_store: ConfigStore | None = ConfigStore(self.repo_root) if self.repo_root else None
        self._busy = False
        self._busy_label = ""
        self._config_busy = False
        self._output_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._logger = logging.getLogger(_CONSOLE_LOGGER_NAME)
        self._console_log_path: Path | None = None
        self._viewer_combo: ttk.Combobox | None = None
        self._viewer_text: scrolledtext.ScrolledText | None = None
        self._viewer_sources: list[tuple[str, Path]] = []
        self._viewer_pos: dict[str, int] = {}
        self._viewer_filter = tk.StringVar(value="")
        self._log_mode = "activity"
        self._activity_lines: list[tuple[str, str]] = []
        self._system_log_button: ttk.Button | None = None
        self._system_log_refresh_button: ttk.Button | None = None
        self._poll_seconds = tk.DoubleVar(value=STATUS_POLL_SECONDS)
        self._status_cards: dict[str, tuple[tk.Label, tk.Label, tk.Label]] = {}
        self._nav_widgets: dict[str, tuple[tk.Frame, tk.Label, tk.Label]] = {}
        self._page_key = "gateway"
        self._settings_cache: dict[str, Any] = {}
        self._gateway_cache: dict[str, Any] = {}
        self._settings_cache_dirty = False
        self._gateway_cache_dirty = False
        self._form_vars: dict[tuple[str, str], tuple[tk.Variable, str, Any]] = {}
        self._secret_original: dict[tuple[str, str], str] = {}
        self._provider_rows: list[dict[str, Any]] = []
        self._channel_rows: list[dict[str, Any]] = []
        self._channel_page_mode = "full"
        self._gateway_scalar_vars: dict[str, tuple[tk.Variable, str, Any]] = {}
        self._config_inner: tk.Frame | None = None
        self._last_gateway_error: str | None = None
        self._dashboard_layout_mode: str | None = None
        self._layout_refresh_pending = False

        self._setup_console_logger(self.repo_root)
        self._build_vars()
        self._configure_styles()
        self.controller = StackController(self.repo_root, self._queue_event, self._logger)
        self._build_ui()
        self._update_repo_label()
        self._schedule_poll(0.35)
        self._pump_queue()
        self._log_console(logging.INFO, f"控制台启动（repo_root={self.repo_root}）")
        self._log("info", "网关监控已启动。配置中心支持在线热应用和离线原子保存。")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- setup ----------

    def _setup_console_logger(self, repo_root: Path | None) -> None:
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers.clear()
        path = repo_root / "output" / "console.log" if repo_root else Path.home() / ".ashare_trader_console.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=CONSOLE_LOG_MAX_BYTES, backupCount=CONSOLE_LOG_BACKUPS, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            self._logger.addHandler(handler)
            self._console_log_path = path
        except OSError as exc:
            self._console_log_path = None
            self._logger.addHandler(logging.NullHandler())
            self._logger.warning("无法创建控制台日志文件 %s: %s", path, exc)

    def _log_console(self, level: int, message: str, **kwargs: object) -> None:
        try:
            self._logger.log(level, message, **kwargs)
        except Exception:  # noqa: BLE001 - logging must never impact the UI
            pass

    def _build_vars(self) -> None:
        self.var_status = tk.StringVar(value="已停止")
        self.var_status_detail = tk.StringVar(value="等待健康检查")
        self.var_mode = tk.StringVar(value="—")
        self.var_api = tk.StringVar(value="未检测")
        self.var_gateway = tk.StringVar(value="未检测")
        self.var_local_model = tk.StringVar(value="未检测")
        self.var_quote_bridge = tk.StringVar(value="未检测")
        self.var_news_bridge = tk.StringVar(value="未检测")
        self.var_last_check = tk.StringVar(value="尚未检查")
        self.var_last_action = tk.StringVar(value="—")
        self.var_log_title = tk.StringVar(value="LIVE ACTIVITY")
        self.var_full_build = tk.BooleanVar(value=False)
        self.var_auto_scroll = tk.BooleanVar(value=True)
        self.var_expert_mode = tk.BooleanVar(value=False)
        self.var_virtual_trading = tk.BooleanVar(value=True)
        self.var_page_title = tk.StringVar(value="网关监控")
        self.var_page_subtitle = tk.StringVar(value=PAGE_SUBTITLES["gateway"])
        self.var_page_status = tk.StringVar(value="")
        self.var_gateway_port = tk.StringVar(value="—")
        self.var_gateway_uptime = tk.StringVar(value="—")
        self.var_gateway_channels = tk.StringVar(value="—")
        self.var_gateway_version = tk.StringVar(value="—")
        self.var_gateway_pid = tk.StringVar(value="PID  —")
        self.var_binary_path = tk.StringVar(value="—")
        self.var_data_dir = tk.StringVar(value="—")
        self.var_settings_path = tk.StringVar(value="—")
        self.var_gateway_config_path = tk.StringVar(value="—")
        self._load_runtime_preferences()

    def _load_runtime_preferences(self) -> None:
        if not self.config_store:
            return
        try:
            payload = self.config_store.read_settings()
        except ConfigError:
            return
        profile = payload.get("profile") if isinstance(payload, dict) else {}
        trading = payload.get("trading") if isinstance(payload, dict) else {}
        if isinstance(profile, Mapping):
            self.var_expert_mode.set(bool(profile.get("expert_mode", False)))
        if isinstance(trading, Mapping):
            self.var_virtual_trading.set(bool(trading.get("virtual_trading", True)))

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Console.TButton", background=COLORS["surface_2"], foreground=COLORS["text"], borderwidth=0, padding=(13, 10), font=(UI_FONT, 11))
        style.map("Console.TButton", background=[("active", COLORS["surface_3"]), ("disabled", "#17263b")], foreground=[("disabled", COLORS["faint"])])
        style.configure("Primary.TButton", background=COLORS["cyan"], foreground="#06101b", borderwidth=0, padding=(16, 10), font=(UI_FONT, 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#8be6ff"), ("disabled", "#31586a")])
        style.configure("Danger.TButton", background="#402535", foreground=COLORS["red"], borderwidth=0, padding=(15, 10), font=(UI_FONT, 11, "bold"))
        style.map("Danger.TButton", background=[("active", "#5b2d43"), ("disabled", "#2b2330")])
        style.configure("Ghost.TButton", background=COLORS["surface"], foreground=COLORS["muted"], borderwidth=0, padding=(11, 9), font=(UI_FONT, 11))
        style.map("Ghost.TButton", background=[("active", COLORS["surface_2"])])
        style.configure("Console.TCheckbutton", background=COLORS["surface"], foreground=COLORS["muted"], font=(UI_FONT, 10), padding=4)
        style.map("Console.TCheckbutton", background=[("active", COLORS["surface"])], foreground=[("active", COLORS["text"])])
        style.configure("Console.TRadiobutton", background=COLORS["surface"], foreground=COLORS["muted"], font=(UI_FONT, 10), padding=4)
        style.map("Console.TRadiobutton", background=[("active", COLORS["surface"])], foreground=[("active", COLORS["text"])])
        style.configure("Console.TCombobox", fieldbackground=COLORS["surface_2"], background=COLORS["surface_2"], foreground=COLORS["text"], borderwidth=0, padding=6, font=(UI_FONT, 11))
        style.configure("Console.TEntry", fieldbackground=COLORS["surface_2"], foreground=COLORS["text"], borderwidth=0, padding=6, font=(UI_FONT, 11))
        style.configure("Console.Vertical.TScrollbar", troughcolor=COLORS["bg"], background=COLORS["surface_2"], borderwidth=0)

    # ---------- shared UI ----------

    def _card(self, parent: tk.Misc, **kwargs: object) -> tk.Frame:
        return tk.Frame(parent, bg=COLORS["surface"], highlightthickness=1, highlightbackground=COLORS["line"], **kwargs)

    def _label(self, parent: tk.Misc, text: str = "", **kwargs: object) -> tk.Label:
        background = kwargs.pop("bg", COLORS["surface"])
        foreground = kwargs.pop("fg", COLORS["text"])
        font_value = kwargs.pop("font", (UI_FONT, 10))
        if isinstance(font_value, (tuple, list)) and len(font_value) >= 2:
            parts = list(font_value)
            try:
                size = int(parts[1])
                # The old dashboard used 8/9pt labels. Keep headings intact,
                # but make all supporting Chinese text readable at 100% DPI.
                if size <= 9:
                    parts[1] = 10
                font_value = tuple(parts)
            except (TypeError, ValueError):
                pass
        return tk.Label(parent, text=text, bg=background, fg=foreground, font=font_value, **kwargs)

    def _build_ui(self) -> None:
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        sidebar = tk.Frame(self.root, width=248, bg=COLORS["sidebar"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        self._build_sidebar(sidebar)
        workspace = tk.Frame(self.root, bg=COLORS["bg"])
        workspace.grid(row=0, column=1, sticky="nsew")
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(1, weight=1)
        self._build_header(workspace)
        self.content = tk.Frame(workspace, bg=COLORS["bg"])
        self.content.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 18))
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)
        self._show_page("gateway")
        self.root.bind("<Configure>", self._on_root_configure)

    def _build_sidebar(self, parent: tk.Frame) -> None:
        brand = tk.Frame(parent, bg=COLORS["sidebar"])
        brand.pack(fill=tk.X, padx=24, pady=(18, 18))
        self._label(brand, "◈", bg=COLORS["sidebar"], fg=COLORS["cyan"], font=("Segoe UI Symbol", 25)).pack(side=tk.LEFT)
        titles = tk.Frame(brand, bg=COLORS["sidebar"])
        titles.pack(side=tk.LEFT, padx=(9, 0))
        self._label(titles, "A-SHARE", bg=COLORS["sidebar"], fg=COLORS["text"], font=("Segoe UI", 14, "bold")).pack(anchor="w")
        self._label(titles, "AI TRADER CONSOLE", bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self._label(parent, "WORKSPACE", bg=COLORS["sidebar"], fg=COLORS["faint"], font=(UI_FONT, 10, "bold")).pack(anchor="w", padx=24, pady=(0, 9))
        for key, icon, label in NAV_ITEMS:
            self._sidebar_item(parent, key, icon, label)
        tk.Frame(parent, bg=COLORS["line"], height=1).pack(fill=tk.X, padx=24, pady=12)
        self._label(parent, "STACK STATUS", bg=COLORS["sidebar"], fg=COLORS["faint"], font=(UI_FONT, 10, "bold")).pack(anchor="w", padx=24, pady=(0, 9))
        self.sidebar_status_dot = self._label(parent, "●", bg=COLORS["sidebar"], fg=COLORS["faint"], font=("Segoe UI", 11))
        self.sidebar_status_dot.pack(anchor="w", padx=27)
        self.sidebar_status_text = self._label(parent, bg=COLORS["sidebar"], fg=COLORS["text"], font=("Segoe UI", 10, "bold"), textvariable=self.var_status)
        self.sidebar_status_text.pack(anchor="w", padx=47, pady=(0, 3))
        self._label(parent, bg=COLORS["sidebar"], fg=COLORS["muted"], font=(UI_FONT, 10), wraplength=180, justify=tk.LEFT, textvariable=self.var_status_detail).pack(anchor="w", padx=47)
        bottom = tk.Frame(parent, bg=COLORS["sidebar"])
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=24, pady=19)
        self._label(bottom, "LOCAL CONTROL PLANE", bg=COLORS["sidebar"], fg=COLORS["faint"], font=(UI_FONT, 10)).pack(anchor="w")
        self._label(bottom, "Tkinter · PyInstaller · fail-closed", bg=COLORS["sidebar"], fg=COLORS["muted"], font=(MONO_FONT, 10)).pack(anchor="w", pady=(4, 0))

    def _sidebar_item(self, parent: tk.Frame, key: str, icon: str, label: str) -> None:
        frame = tk.Frame(parent, bg=COLORS["sidebar"], cursor="hand2")
        frame.pack(fill=tk.X, padx=14, pady=0)
        icon_label = self._label(frame, icon, bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Segoe UI Symbol", 14), width=3)
        icon_label.pack(side=tk.LEFT, padx=(5, 0), pady=6)
        text_label = self._label(frame, label, bg=COLORS["sidebar"], fg=COLORS["muted"], font=(UI_FONT, 11))
        text_label.pack(side=tk.LEFT, pady=6)
        self._nav_widgets[key] = (frame, icon_label, text_label)
        for widget in (frame, icon_label, text_label):
            widget.bind("<Button-1>", lambda _event, name=key: self._show_page(name))

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, bg=COLORS["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 17))
        header.columnconfigure(0, weight=1)
        title = tk.Frame(header, bg=COLORS["bg"])
        title.grid(row=0, column=0, sticky="w")
        self._label(title, bg=COLORS["bg"], fg=COLORS["text"], font=(UI_FONT, 24, "bold"), textvariable=self.var_page_title).pack(anchor="w")
        self._label(title, bg=COLORS["bg"], fg=COLORS["muted"], font=(UI_FONT, 11), textvariable=self.var_page_subtitle).pack(anchor="w", pady=(5, 0))
        actions = tk.Frame(header, bg=COLORS["bg"])
        actions.grid(row=0, column=1, sticky="e")
        self.status_label = self._label(actions, bg=COLORS["bg"], fg=COLORS["faint"], font=(UI_FONT, 11, "bold"), textvariable=self.var_status)
        self.status_label.pack(side=tk.LEFT, padx=(0, 19))
        self.btn_refresh = ttk.Button(actions, text="⟳  刷新状态", style="Console.TButton", command=self._refresh_once)
        self.btn_refresh.pack(side=tk.LEFT)

    def _set_active_nav(self, key: str) -> None:
        for name, (frame, icon_label, text_label) in self._nav_widgets.items():
            active = name == key
            background = "#1a3151" if active else COLORS["sidebar"]
            frame.configure(bg=background)
            icon_label.configure(bg=background, fg=COLORS["cyan"] if active else COLORS["muted"])
            text_label.configure(bg=background, fg=COLORS["text"] if active else COLORS["muted"], font=(UI_FONT, 11, "bold" if active else "normal"))

    def _show_page(self, key: str) -> None:
        if key not in PAGE_MAPPINGS:
            return
        if self._log_mode == "system":
            self._set_activity_log_view()
        self._page_key = key
        self._set_active_nav(key)
        self.var_page_title.set(PAGE_MAPPINGS[key]["title"])
        self.var_page_subtitle.set(PAGE_SUBTITLES.get(key, ""))
        self.var_page_status.set("")
        self._status_cards.clear()
        for child in self.content.winfo_children():
            child.destroy()
        self._viewer_combo = None
        self._viewer_text = None
        self._system_log_button = None
        self._system_log_refresh_button = None
        self.log = None  # type: ignore[assignment]
        if key == "gateway":
            self._build_dashboard(self.content)
        elif key in {"channels", "subscriptions"}:
            self._build_gateway_config_page(self.content, "subscription" if key == "subscriptions" else "full")
        elif key == "cockpit":
            self._build_cockpit_page(self.content)
        else:
            self._build_config_page(self.content, key)

    # ---------- dashboard ----------

    def _build_dashboard(self, parent: tk.Frame) -> None:
        self._dashboard_layout_mode = self._dashboard_layout()
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=0, minsize=98)
        parent.rowconfigure(1, weight=0, minsize=55)
        parent.rowconfigure(2, weight=0)
        parent.rowconfigure(3, weight=1, minsize=120)
        parent.rowconfigure(4, weight=0)
        self._build_metric_grid(parent)
        self._build_service_strip(parent)
        self._build_command_area(parent)
        self._build_log_panel(parent)
        if self._dashboard_layout_mode == "wide":
            self._build_footer(parent)

    def _build_footer(self, parent: tk.Frame) -> None:
        footer = tk.Frame(parent, bg=COLORS["bg"])
        footer.grid(row=4, column=0, sticky="ew", pady=(9, 0))
        self._label(footer, "关闭窗口不会停止后台服务 · 使用‘停止全部’释放受控进程", bg=COLORS["bg"], fg=COLORS["faint"], font=("Segoe UI", 8)).pack(side=tk.LEFT)
        poll = tk.Frame(footer, bg=COLORS["bg"])
        poll.pack(side=tk.RIGHT)
        self._label(poll, "健康检查间隔", bg=COLORS["bg"], fg=COLORS["faint"], font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 7))
        self.poll_combo = ttk.Combobox(poll, state="readonly", width=5, values=("2", "3", "5", "10", "30"), style="Console.TCombobox")
        self.poll_combo.set(str(int(self._poll_seconds.get())))
        self.poll_combo.pack(side=tk.LEFT)
        self.poll_combo.bind("<<ComboboxSelected>>", self._change_poll)

    def _build_metric_grid(self, parent: tk.Frame) -> None:
        grid = tk.Frame(parent, bg=COLORS["bg"])
        grid.grid(row=0, column=0, sticky="ew", pady=(0, 13))
        for column in range(4):
            grid.columnconfigure(column, weight=1, uniform="gateway-metric")
        metrics = (("网关端口", self.var_gateway_port, "LISTEN PORT", COLORS["cyan"], "Gateway listener"), ("运行时长", self.var_gateway_uptime, "UPTIME", COLORS["green"], "Live process uptime"), ("调度渠道", self.var_gateway_channels, "CHANNELS", COLORS["purple"], "Healthy / configured"), ("网关版本", self.var_gateway_version, "VERSION", COLORS["amber"], "Gateway build"))
        for index, (title, variable, eyebrow, accent, detail) in enumerate(metrics):
            card = self._card(grid, height=98)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0 if index == 3 else 6))
            card.grid_propagate(False)
            card.columnconfigure(1, weight=1)
            tk.Frame(card, bg=accent, width=4).grid(row=0, column=0, rowspan=3, sticky="nsw")
            self._label(card, eyebrow, fg=COLORS["muted"], font=(UI_FONT, 9, "bold")).grid(row=0, column=1, sticky="w", padx=(17, 8), pady=(14, 2))
            self._label(card, title, fg=COLORS["faint"], font=(UI_FONT, 9)).grid(row=0, column=2, sticky="e", padx=(0, 15), pady=(14, 2))
            self._label(card, textvariable=variable, fg=COLORS["text"], font=(UI_FONT, 19, "bold")).grid(row=1, column=1, sticky="w", padx=(17, 8), pady=(0, 3))
            self._label(card, "●", fg=accent, font=(UI_FONT, 10)).grid(row=1, column=2, sticky="e", padx=(0, 15))
            self._label(card, detail, fg=COLORS["faint"], font=(UI_FONT, 9)).grid(row=2, column=1, columnspan=2, sticky="w", padx=(17, 8), pady=(0, 14))

    def _dashboard_layout(self) -> str:
        try:
            width = self.root.winfo_width()
        except tk.TclError:
            width = 1320
        return "compact" if width and width < COMPACT_WIDTH else "wide"

    def _on_root_configure(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is not self.root or self._layout_refresh_pending:
            return
        if self._page_key != "gateway":
            return
        if self._dashboard_layout_mode == self._dashboard_layout():
            return
        self._layout_refresh_pending = True
        self.root.after_idle(self._refresh_dashboard_layout)

    def _refresh_dashboard_layout(self) -> None:
        self._layout_refresh_pending = False
        if self._page_key == "gateway":
            self._show_page("gateway")

    def _build_service_strip(self, parent: tk.Frame) -> None:
        card = self._card(parent)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 13))
        card.columnconfigure(0, weight=0)
        self._label(card, "服务探测", font=(UI_FONT, 10, "bold")).grid(row=0, column=0, sticky="w", padx=(16, 18), pady=13)
        services = (("api", "控制面", self.var_api), ("gateway", "网关", self.var_gateway), ("local_model", "本地模型", self.var_local_model), ("quote_bridge", "行情桥", self.var_quote_bridge), ("news_bridge", "新闻桥", self.var_news_bridge))
        for column in range(1, len(services) + 1):
            card.columnconfigure(column, weight=1, uniform="service-probe")
        for index, (key, label, variable) in enumerate(services, start=1):
            service = tk.Frame(card, bg=COLORS["surface_2"])
            service.grid(row=0, column=index, sticky="ew", padx=(0, 6), pady=7)
            service.columnconfigure(1, weight=1)
            dot = self._label(service, "●", fg=COLORS["faint"], font=(UI_FONT, 10))
            dot.grid(row=0, column=0, padx=(9, 4), pady=8)
            value = self._label(service, label, fg=COLORS["muted"], font=(UI_FONT, 10))
            value.grid(row=0, column=1, sticky="w", pady=8)
            detail = self._label(service, textvariable=variable, fg=COLORS["faint"], font=(UI_FONT, 9))
            detail.grid(row=0, column=2, sticky="e", padx=(4, 9), pady=8)
            self._status_cards[key] = (value, detail, dot)

    def _build_command_area(self, parent: tk.Frame) -> None:
        compact = self._dashboard_layout() == "compact"
        area = tk.Frame(parent, bg=COLORS["bg"])
        area.grid(row=2, column=0, sticky="ew", pady=(0, 13))
        area.columnconfigure(0, weight=1, uniform="dashboard-command")
        area.columnconfigure(1, weight=1, uniform="dashboard-command")
        commands = self._card(area)
        if compact:
            commands.grid(row=0, column=0, columnspan=2, sticky="ew")
        else:
            commands.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        commands.columnconfigure(0, weight=1)
        top = tk.Frame(commands, bg=COLORS["surface"])
        top.grid(row=0, column=0, sticky="ew", padx=17, pady=(13, 8))
        self._label(top, "服务启停与重启", fg=COLORS["muted"], font=(UI_FONT, 10, "bold")).pack(side=tk.LEFT)
        self._label(top, textvariable=self.var_mode, fg=COLORS["cyan"], font=(UI_FONT, 10, "bold")).pack(side=tk.RIGHT)
        buttons = tk.Frame(commands, bg=COLORS["surface"])
        buttons.grid(row=1, column=0, sticky="ew", padx=17)
        button_specs = (
            ("btn_start", "▶  启动全部", "Primary.TButton", self._start),
            ("btn_stop", "■  停止全部", "Danger.TButton", self._stop),
            ("btn_restart", "↻  重启全部", "Console.TButton", self._restart),
            ("btn_model_start", "▶ 仅启动模型", "Console.TButton", self._start_local_model),
            ("btn_model_stop", "■ 仅停止模型", "Ghost.TButton", self._stop_local_model),
        )
        for index, (attribute, text, style, command) in enumerate(button_specs):
            button = ttk.Button(buttons, text=text, style=style, command=command)
            setattr(self, attribute, button)
            column = index if compact else index % 3
            row = 0 if compact else index // 3
            buttons.columnconfigure(column, weight=1, uniform="command-button")
            button.grid(row=row, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0), pady=(0 if row == 0 else 6, 0))
        options = tk.Frame(commands, bg=COLORS["surface"])
        options.grid(row=2, column=0, sticky="ew", padx=17, pady=(9, 13))
        option_specs = (
            ("完整构建 npm + cargo", self.var_full_build, None),
            ("本地虚拟盘", self.var_virtual_trading, self._save_operator_preferences),
        )
        for index, (text, variable, command) in enumerate(option_specs):
            widget = ttk.Checkbutton(options, text=text, variable=variable, style="Console.TCheckbutton", command=command)
            if compact:
                widget.pack(side=tk.LEFT, padx=(0 if index == 0 else 13, 0))
            else:
                widget.pack(side=tk.LEFT, padx=(0 if index == 0 else 13, 0))
        fool = ttk.Radiobutton(options, text="傻瓜", value=False, variable=self.var_expert_mode, command=self._save_operator_preferences, style="Console.TRadiobutton")
        expert = ttk.Radiobutton(options, text="专家", value=True, variable=self.var_expert_mode, command=self._save_operator_preferences, style="Console.TRadiobutton")
        if compact:
            fool.pack(side=tk.LEFT, padx=(12, 0))
            expert.pack(side=tk.LEFT)
        else:
            fool.pack(side=tk.LEFT, padx=(12, 0))
            expert.pack(side=tk.LEFT)
        last_action = self._label(options, textvariable=self.var_last_action, fg=COLORS["faint"], font=(MONO_FONT, 9))
        if compact:
            last_action.pack(side=tk.RIGHT, padx=(12, 0))
        else:
            last_action.pack(side=tk.RIGHT)
        paths = self._card(area)
        if compact:
            # At the minimum window width the path panel gets its own row so
            # it cannot overlap the lifecycle controls.
            paths.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        else:
            paths.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        paths.columnconfigure(1, weight=1)
        if compact:
            paths.columnconfigure(3, weight=1)
        self._label(paths, "运行路径", fg=COLORS["muted"], font=(UI_FONT, 10, "bold")).grid(row=0, column=0, columnspan=(2 if compact else 2), sticky="w", padx=17, pady=(13, 9))
        path_items = (("二进制路径", self.var_binary_path), ("数据目录", self.var_data_dir), ("配置路径", self.var_settings_path), ("网关配置", self.var_gateway_config_path))
        if compact:
            for index, (label, variable) in enumerate(path_items):
                row = 1 + index // 2
                label_col = (index % 2) * 2
                value_col = label_col + 1
                self._label(paths, label, fg=COLORS["faint"], font=(UI_FONT, 10)).grid(row=row, column=label_col, sticky="nw", padx=(17, 8), pady=3)
                self._label(paths, textvariable=variable, fg=COLORS["text"], font=(MONO_FONT, 9), anchor="w", justify=tk.LEFT, wraplength=350).grid(row=row, column=value_col, sticky="ew", padx=(0, 15), pady=3)
            self._label(paths, textvariable=self.var_gateway_pid, fg=COLORS["faint"], font=(MONO_FONT, 9)).grid(row=0, column=2, columnspan=2, sticky="e", padx=(0, 17), pady=(13, 9))
        else:
            for row, (label, variable) in enumerate(path_items, start=1):
                self._label(paths, label, fg=COLORS["faint"], font=(UI_FONT, 10)).grid(row=row, column=0, sticky="nw", padx=(17, 10), pady=4)
                self._label(paths, textvariable=variable, fg=COLORS["text"], font=(MONO_FONT, 10), anchor="w", justify=tk.LEFT, wraplength=430).grid(row=row, column=1, sticky="ew", padx=(0, 15), pady=4)
            self._label(paths, textvariable=self.var_gateway_pid, fg=COLORS["faint"], font=(MONO_FONT, 10)).grid(row=5, column=0, columnspan=2, sticky="w", padx=17, pady=(5, 13))

    # ---------- log panel ----------

    def _build_log_panel(self, parent: tk.Frame) -> None:
        panel = self._card(parent)
        panel.grid(row=3, column=0, sticky="nsew")
        panel.rowconfigure(2, weight=1)
        panel.columnconfigure(0, weight=1)
        toolbar = tk.Frame(panel, bg=COLORS["surface"])
        toolbar.grid(row=0, column=0, sticky="ew", padx=15, pady=(10, 4))
        self._label(toolbar, textvariable=self.var_log_title, fg=COLORS["muted"], font=(UI_FONT, 10, "bold")).pack(side=tk.LEFT)
        self._label(toolbar, textvariable=self.var_last_check, fg=COLORS["faint"], font=(UI_FONT, 9)).pack(side=tk.LEFT, padx=(13, 0))
        self._system_log_button = ttk.Button(toolbar, text="系统日志", style="Ghost.TButton", command=self._open_log_viewer)
        self._system_log_button.pack(side=tk.RIGHT)

        controls = tk.Frame(panel, bg=COLORS["surface"])
        controls.grid(row=1, column=0, sticky="ew", padx=15, pady=(0, 7))
        self._label(controls, "日志源", fg=COLORS["faint"], font=(UI_FONT, 9)).pack(side=tk.LEFT, padx=(0, 5))
        self._viewer_combo = ttk.Combobox(controls, state="readonly", width=21, style="Console.TCombobox")
        self._viewer_combo.bind("<<ComboboxSelected>>", lambda _event: self._viewer_refresh())
        self._viewer_combo.pack_forget()
        self._system_log_refresh_button = ttk.Button(controls, text="刷新日志", style="Ghost.TButton", command=self._viewer_refresh)
        self._system_log_refresh_button.pack_forget()
        self._label(controls, "搜索", fg=COLORS["faint"], font=(UI_FONT, 9)).pack(side=tk.RIGHT, padx=(5, 4))
        self._system_log_filter_entry = ttk.Entry(controls, textvariable=self._viewer_filter, width=18, style="Console.TEntry")
        self._system_log_filter_entry.bind("<Return>", lambda _event: self._refresh_log_search())
        self._system_log_filter_entry.pack(side=tk.RIGHT)
        ttk.Checkbutton(controls, text="自动滚动", variable=self.var_auto_scroll, style="Console.TCheckbutton").pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(controls, text="复制", style="Ghost.TButton", command=self._copy_selected_log).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(controls, text="清空", style="Ghost.TButton", command=self._clear_log).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(controls, text="保存", style="Ghost.TButton", command=self._save_log_snapshot).pack(side=tk.RIGHT, padx=(5, 0))
        log_frame = tk.Frame(panel, bg="#07101d")
        log_frame.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 15))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(log_frame, state=tk.DISABLED, font=(MONO_FONT, 10), wrap=tk.WORD, bg="#07101d", fg="#b9c8dc", insertbackground=COLORS["text"], relief=tk.FLAT, borderwidth=0, padx=13, pady=11)
        self.log.grid(row=0, column=0, sticky="nsew")
        for name, color in (("info", "#b9c8dc"), ("warn", COLORS["amber"]), ("error", COLORS["red"]), ("success", COLORS["green"]), ("muted", COLORS["faint"])):
            self.log.tag_configure(name, foreground=color)
        self.log.tag_configure("match", background="#2c4d70", foreground=COLORS["text"])
        self._viewer_text = self.log
        self.log.bind("<Control-a>", lambda _event: (self.log.tag_add(tk.SEL, "1.0", tk.END), "break")[1])
        self.log.bind("<Button-3>", self._show_log_context)
        self._render_activity_log()

    def _refresh_log_search(self) -> None:
        if self._log_mode == "system":
            self._viewer_refresh()
        else:
            self._render_activity_log()

    def _log(self, level: str, message: str) -> None:
        line = str(message).rstrip()
        self._activity_lines.append((level, line))
        if len(self._activity_lines) > 2000:
            del self._activity_lines[: len(self._activity_lines) - 2000]
        if self._log_mode != "activity" or not getattr(self, "log", None):
            return
        query = self._viewer_filter.get().strip().lower()
        if query and query not in line.lower():
            return
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line + "\n", level)
        if self.var_auto_scroll.get():
            self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _render_activity_log(self) -> None:
        if not getattr(self, "log", None):
            return
        query = self._viewer_filter.get().strip().lower()
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        for level, line in self._activity_lines:
            if query and query not in line.lower():
                continue
            self.log.insert(tk.END, line + "\n", level)
        self.log.configure(state=tk.DISABLED)
        self.log.see(tk.END)

    def _clear_log(self) -> None:
        if self._log_mode == "system":
            self._viewer_refresh()
            return
        self._activity_lines.clear()
        if getattr(self, "log", None):
            self.log.configure(state=tk.NORMAL)
            self.log.delete("1.0", tk.END)
            self.log.configure(state=tk.DISABLED)
        self._log("info", "日志窗口已清空（文件日志仍然保留）")

    def _save_log_snapshot(self) -> None:
        if not getattr(self, "log", None):
            return
        target = filedialog.asksaveasfilename(title="保存日志快照", defaultextension=".log", filetypes=(("日志文件", "*.log"), ("文本文件", "*.txt")))
        if not target:
            return
        try:
            Path(target).write_text(self.log.get("1.0", tk.END), encoding="utf-8")
            self._log("success", f"日志快照已保存: {target}")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"保存日志失败: {exc}")

    def _show_log_context(self, _event: object) -> None:
        if not getattr(self, "log", None):
            return
        menu = tk.Menu(self.root, tearoff=False, bg=COLORS["surface_2"], fg=COLORS["text"], activebackground=COLORS["surface_3"], activeforeground=COLORS["text"])
        menu.add_command(label="复制选中内容", command=self._copy_selected_log)
        menu.add_command(label="全选", command=lambda: self.log.tag_add(tk.SEL, "1.0", tk.END))
        menu.add_separator()
        menu.add_command(label="清空窗口", command=self._clear_log)
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def _copy_selected_log(self) -> None:
        if not getattr(self, "log", None):
            return
        try:
            content = self.log.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self._log("success", "已复制选中日志")

    # ---------- system log viewer ----------

    def _log_sources(self) -> list[tuple[str, Path]]:
        sources: list[tuple[str, Path]] = []
        seen: set[str] = set()

        def add(name: str, path: Path) -> None:
            if str(path) not in seen:
                seen.add(str(path))
                sources.append((name, path))

        if self.repo_root is not None:
            for name, rel in (("API 输出", ".run/api.out.log"), ("API 错误", ".run/api.err.log"), ("Gateway 输出", ".run/gateway.out.log"), ("Gateway 错误", ".run/gateway.err.log"), ("Quote Bridge", ".run/bridge.out.log"), ("News Bridge", ".run/newsbridge.out.log"), ("Trader 日志", "a_share_ai_trader/logs/trader.log")):
                add(name, self.repo_root / rel)
            output_dir = self.repo_root / "output"
            if output_dir.is_dir():
                for path in sorted(output_dir.glob("*.log")):
                    add(f"output/{path.name}", path)
        add("控制台日志", self._console_log_path or Path.home() / ".ashare_trader_console.log")
        return sources

    def _open_log_viewer(self) -> None:
        if self._log_mode == "system":
            self._set_activity_log_view()
        else:
            self._set_system_log_view()

    def _viewer_alive(self) -> bool:
        return self._log_mode == "system" and self._viewer_combo is not None and self._viewer_text is not None and getattr(self, "log", None) is not None

    def _viewer_tick(self) -> None:
        if not self._viewer_alive():
            return
        self._viewer_refresh()
        try:
            self.root.after(LOG_VIEWER_REFRESH_MS, self._viewer_tick)
        except tk.TclError:
            pass

    def _viewer_refresh(self) -> None:
        if not self._viewer_alive() or self._viewer_combo is None or self._viewer_text is None or not self._viewer_sources:
            return
        index = self._viewer_combo.current()
        if index < 0 or index >= len(self._viewer_sources):
            return
        _name, path = self._viewer_sources[index]
        text_widget = self._viewer_text
        try:
            at_bottom = text_widget.yview()[1] >= 0.999
            size = path.stat().st_size
            position = self._viewer_pos.get(str(path), 0)
            if position > size or not position:
                position = max(0, size - LOG_VIEWER_MAX_BYTES)
                with open(path, "rb") as stream:
                    stream.seek(position)
                    content = decode_log_bytes(stream.read(LOG_VIEWER_MAX_BYTES))
                text_widget.configure(state=tk.NORMAL)
                text_widget.delete("1.0", tk.END)
                text_widget.insert(tk.END, content or f"（日志为空: {path}）\n")
                text_widget.configure(state=tk.DISABLED)
                self._viewer_pos[str(path)] = size
            elif size > position:
                with open(path, "rb") as stream:
                    stream.seek(position)
                    content = decode_log_bytes(stream.read(size - position))
                text_widget.configure(state=tk.NORMAL)
                text_widget.insert(tk.END, content)
                text_widget.configure(state=tk.DISABLED)
                self._viewer_pos[str(path)] = size
            text_widget.tag_remove("match", "1.0", tk.END)
            query = self._viewer_filter.get().strip()
            if query:
                cursor = "1.0"
                while True:
                    found = text_widget.search(query, cursor, stopindex=tk.END, nocase=True)
                    if not found:
                        break
                    end = f"{found}+{len(query)}c"
                    text_widget.tag_add("match", found, end)
                    cursor = end
            if at_bottom:
                text_widget.see(tk.END)
        except OSError:
            text_widget.configure(state=tk.NORMAL)
            text_widget.delete("1.0", tk.END)
            text_widget.insert(tk.END, f"（日志文件不存在: {path}）\n", "muted")
            text_widget.configure(state=tk.DISABLED)
            self._viewer_pos[str(path)] = 0

    def _set_system_log_view(self) -> None:
        if not getattr(self, "log", None):
            return
        self._log_mode = "system"
        self.var_log_title.set("SYSTEM LOGS")
        self._viewer_sources = self._log_sources()
        self._viewer_pos = {}
        self._viewer_text = self.log
        if self._viewer_combo is not None:
            self._viewer_combo.configure(values=[name for name, _ in self._viewer_sources])
            if self._viewer_sources:
                self._viewer_combo.current(0)
            self._viewer_combo.pack(side=tk.LEFT, padx=(12, 0))
        if self._system_log_refresh_button is not None:
            self._system_log_refresh_button.pack(side=tk.LEFT, padx=(5, 0))
        if self._system_log_button is not None:
            self._system_log_button.configure(text="返回实时")
        self._viewer_refresh()
        try:
            self.root.after(LOG_VIEWER_REFRESH_MS, self._viewer_tick)
        except tk.TclError:
            pass

    def _set_activity_log_view(self) -> None:
        self._log_mode = "activity"
        self.var_log_title.set("LIVE ACTIVITY")
        if self._viewer_combo is not None:
            self._viewer_combo.pack_forget()
        if self._system_log_refresh_button is not None:
            self._system_log_refresh_button.pack_forget()
        if self._system_log_button is not None:
            self._system_log_button.configure(text="系统日志")
        self._render_activity_log()

    # ---------- configuration pages ----------

    def _new_scroll_area(self, parent: tk.Frame) -> tk.Frame:
        outer = tk.Frame(parent, bg=COLORS["bg"])
        outer.grid(row=0, column=0, sticky="nsew")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, bg=COLORS["bg"], highlightthickness=0, borderwidth=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview, style="Console.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = tk.Frame(canvas, bg=COLORS["bg"])
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        self._config_inner = inner
        return inner

    def _page_toolbar(self, parent: tk.Frame, save_command: object) -> None:
        toolbar = self._card(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        toolbar.columnconfigure(0, weight=1)
        self._label(toolbar, "CONFIGURATION CENTER", fg=COLORS["muted"], font=("Segoe UI", 8, "bold")).grid(row=0, column=0, sticky="w", padx=15, pady=11)
        self.page_status_label = self._label(toolbar, textvariable=self.var_page_status, fg=COLORS["faint"], font=("Segoe UI", 9))
        self.page_status_label.grid(row=0, column=1, sticky="e", padx=(5, 10))
        ttk.Button(toolbar, text="重新读取", style="Ghost.TButton", command=self._reload_config_page).grid(row=0, column=2, padx=(0, 5), pady=5)
        self.page_save_button = ttk.Button(toolbar, text="保存当前页", style="Primary.TButton", command=save_command)
        self.page_save_button.grid(row=0, column=3, padx=(0, 9), pady=5)

    def _build_config_page(self, parent: tk.Frame, key: str) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        inner = self._new_scroll_area(parent)
        inner.columnconfigure(0, weight=1)
        self._page_toolbar(inner, self._save_current_page)
        if not self.config_store:
            self._empty_config_message(inner, "未定位仓库，请先在管理面板选择包含 scripts\\service_control.ps1 的目录。", True)
            return
        if not self._settings_cache_dirty:
            try:
                self._settings_cache = self.config_store.read_settings()
            except ConfigError as exc:
                self._set_page_status(f"读取失败：{exc}", "error")
                self._empty_config_message(inner, str(exc), True)
                return
        self._form_vars.clear()
        self._secret_original.clear()
        self._provider_rows.clear()
        row = 1
        for module in PAGE_MAPPINGS[key]["modules"]:
            row = self._render_module(inner, module, self._settings_cache, row)
        if key == "admin":
            self._render_admin_tools(inner, row)
        self._set_page_status("已从磁盘读取", "ok")

    def _empty_config_message(self, parent: tk.Frame, text: str, error: bool = False) -> None:
        card = self._card(parent)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._label(card, text, fg=COLORS["red"] if error else COLORS["muted"], font=("Segoe UI", 10), wraplength=760, justify=tk.LEFT).pack(anchor="w", padx=18, pady=18)

    def _render_module(self, parent: tk.Frame, module: str, settings: Mapping[str, Any], start_row: int) -> int:
        values = settings.get(module, {}) if isinstance(settings, Mapping) else {}
        if not isinstance(values, Mapping):
            values = {}
        card = self._card(parent)
        card.grid(row=start_row, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure(1, weight=1)
        self._label(card, module.upper(), fg=COLORS["cyan"], font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(13, 4))
        self._label(card, "字段来自 config/settings.json；保存后会返回热应用或重启提示。", fg=COLORS["faint"], font=("Segoe UI", 8)).grid(row=1, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 9))
        row = 2
        for name, value in values.items():
            if module == "llm" and name == "providers":
                row = self._render_provider_editor(card, value, row)
                continue
            display_value = value
            if isinstance(value, (Mapping, list, tuple)):
                display_value = self._display_complex(value)
            field_type = infer_field_type(module, name, value)
            label = self._label(card, name, fg=COLORS["muted"], font=("Segoe UI", 9), anchor="w")
            label.grid(row=row, column=0, sticky="w", padx=(16, 10), pady=5)
            variable, widget = self._field_widget(card, module, name, display_value, field_type)
            widget.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=4)
            hint = SELECT_OPTIONS.get((module, name), ())
            hint_text = " / ".join(hint) if hint else ("布尔开关" if field_type == "bool" else "")
            self._label(card, hint_text, fg=COLORS["faint"], font=("Segoe UI", 8), anchor="w").grid(row=row, column=2, sticky="w", padx=(0, 16), pady=4)
            self._form_vars[(module, name)] = (variable, field_type, value)
            if field_type == "secret":
                self._secret_original[(module, name)] = str(value or "")
            row += 1
        return row

    def _field_widget(self, parent: tk.Frame, module: str, name: str, value: Any, field_type: str) -> tuple[tk.Variable, tk.Widget]:
        if field_type == "bool":
            variable = tk.BooleanVar(value=bool(value))
            return variable, ttk.Checkbutton(parent, variable=variable, style="Console.TCheckbutton")
        if field_type == "select":
            variable = tk.StringVar(value=str(value))
            return variable, ttk.Combobox(parent, textvariable=variable, state="readonly", values=SELECT_OPTIONS.get((module, name), ()), style="Console.TCombobox")
        if isinstance(value, (list, tuple)):
            display = ",".join(str(item) for item in value)
        else:
            display = str(value or "")
        if field_type == "secret":
            display = mask_secret(display)
        variable = tk.StringVar(value=display)
        return variable, ttk.Entry(parent, textvariable=variable, style="Console.TEntry")

    @staticmethod
    def _display_complex(value: Any) -> str:
        if isinstance(value, Mapping):
            return "; ".join(f"{key}={item}" for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        return str(value)

    def _render_provider_editor(self, parent: tk.Frame, providers: Any, start_row: int) -> int:
        if not isinstance(providers, list):
            providers = []
        self._label(parent, "模型供应商列表", fg=COLORS["muted"], font=("Segoe UI", 9), anchor="w").grid(row=start_row, column=0, columnspan=2, sticky="w", padx=16, pady=(5, 4))
        frame = tk.Frame(parent, bg=COLORS["surface_2"])
        frame.grid(row=start_row + 1, column=0, columnspan=3, sticky="ew", padx=16, pady=(0, 8))
        self._provider_rows = []
        for index, provider in enumerate(providers):
            if not isinstance(provider, Mapping):
                continue
            row: dict[str, Any] = {"original": copy.deepcopy(dict(provider)), "vars": {}}
            card = tk.Frame(frame, bg=COLORS["surface"], highlightthickness=1, highlightbackground=COLORS["line"])
            card.grid(row=index, column=0, columnspan=3, sticky="ew", padx=8, pady=(8 if index == 0 else 4, 4))
            for col in range(6):
                card.columnconfigure(col, weight=1 if col in {1, 3, 5} else 0)
            fields = (("name", "名称"), ("base_url", "Base URL"), ("model", "模型"), ("api_key", "API Key"), ("timeout_seconds", "超时"), ("max_retries", "重试"))
            for col, (field, label_text) in enumerate(fields):
                row_index = 0 if col < 3 else 1
                label_col = (col % 3) * 2
                value_col = label_col + 1
                self._label(card, label_text, fg=COLORS["faint"], font=("Segoe UI", 8)).grid(row=row_index, column=label_col, sticky="w", padx=(8, 4), pady=5)
                original = provider.get(field, "")
                field_type = "secret" if field == "api_key" else ("number" if field in {"timeout_seconds", "max_retries"} else "text")
                variable, widget = self._field_widget(card, "llm", field, original, field_type)
                widget.grid(row=row_index, column=value_col, sticky="ew", padx=(0, 8), pady=4)
                row["vars"][field] = (variable, field_type)
            optional = tk.BooleanVar(value=bool(provider.get("api_key_optional", False)))
            row["vars"]["api_key_optional"] = (optional, "bool")
            ttk.Checkbutton(card, text="允许无密钥", variable=optional, style="Console.TCheckbutton").grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 5))
            ttk.Button(card, text="删除供应商", style="Danger.TButton", command=lambda item=row: self._remove_provider_row(item)).grid(row=2, column=4, columnspan=2, sticky="e", padx=8, pady=(0, 5))
            self._provider_rows.append(row)
        ttk.Button(frame, text="＋ 添加供应商", style="Ghost.TButton", command=self._add_provider_row).grid(row=len(self._provider_rows), column=0, sticky="w", padx=8, pady=(3, 9))
        return start_row + 2

    def _add_provider_row(self) -> None:
        providers = self._settings_cache.setdefault("llm", {}).setdefault("providers", [])
        if isinstance(providers, list):
            providers.append({"name": f"provider-{len(providers) + 1}", "base_url": "", "api_key": "", "model": "", "timeout_seconds": 15, "max_retries": 1, "api_key_optional": False})
        self._settings_cache_dirty = True
        self._show_page(self._page_key)

    def _remove_provider_row(self, row: Mapping[str, Any]) -> None:
        providers = self._settings_cache.get("llm", {}).get("providers", [])
        index = next((i for i, item in enumerate(self._provider_rows) if item is row), None)
        if isinstance(providers, list) and index is not None and index < len(providers):
            providers.pop(index)
        self._settings_cache_dirty = True
        self._show_page(self._page_key)

    # ---------- gateway/channel pages ----------

    def _build_gateway_config_page(self, parent: tk.Frame, mode: str) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        inner = self._new_scroll_area(parent)
        inner.columnconfigure(0, weight=1)
        self._page_toolbar(inner, self._save_current_page)
        self._channel_page_mode = mode
        self._gateway_scalar_vars.clear()
        self._channel_rows.clear()
        if not self.config_store:
            self._empty_config_message(inner, "未定位仓库，请先在管理面板选择仓库。", True)
            return
        if not self._gateway_cache_dirty:
            try:
                self._gateway_cache = self.config_store.read_gateway()
            except ConfigError as exc:
                self._set_page_status(f"读取失败：{exc}", "error")
                self._empty_config_message(inner, str(exc), True)
                return
        scalar_keys = ["default_model"] if mode == "subscription" else ["listen", "default_model", "client_api_key_env", "max_body_bytes", "failure_threshold", "recovery_seconds", "health_probe_seconds", "key_failure_cooldown_seconds", "protocol_fallback"]
        scalar = self._card(inner)
        scalar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        scalar.columnconfigure(1, weight=1)
        self._label(scalar, "GATEWAY SCALAR CONFIG", fg=COLORS["cyan"], font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(13, 8))
        for row, name in enumerate(scalar_keys, start=1):
            value = self._gateway_cache.get(name, "")
            field_type = "bool" if name == "protocol_fallback" else ("number" if name in {"max_body_bytes", "failure_threshold", "recovery_seconds", "health_probe_seconds", "key_failure_cooldown_seconds"} else "text")
            variable, widget = self._field_widget(scalar, "gateway", name, value, field_type)
            self._label(scalar, name, fg=COLORS["muted"], font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", padx=(16, 10), pady=4)
            widget.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=4)
            self._gateway_scalar_vars[name] = (variable, field_type, value)
        channels_card = self._card(inner)
        channels_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        channels_card.columnconfigure(0, weight=1)
        heading = tk.Frame(channels_card, bg=COLORS["surface"])
        heading.grid(row=0, column=0, sticky="ew", padx=16, pady=(13, 5))
        self._label(heading, "渠道列表", fg=COLORS["cyan"], font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        self._label(heading, "优先级越小越优先；密钥仅编辑环境变量名", fg=COLORS["faint"], font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(heading, text="＋ 添加渠道", style="Ghost.TButton", command=self._add_channel_row).pack(side=tk.RIGHT)
        self._channel_rows_frame = tk.Frame(channels_card, bg=COLORS["surface"])
        self._channel_rows_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))
        self._channel_rows_frame.columnconfigure(0, weight=1)
        for index, channel in enumerate(self._gateway_cache.get("channels", [])):
            if isinstance(channel, Mapping):
                self._render_channel_row(self._channel_rows_frame, dict(channel), index, mode)
        self._set_page_status("已从磁盘读取", "ok")

    def _render_channel_row(self, parent: tk.Frame, channel: dict[str, Any], index: int, mode: str) -> None:
        row_data: dict[str, Any] = {"original": copy.deepcopy(channel), "vars": {}}
        card = tk.Frame(parent, bg=COLORS["surface_2"], highlightthickness=1, highlightbackground=COLORS["line"])
        card.grid(row=index, column=0, sticky="ew", pady=(0, 8))
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)
        header = tk.Frame(card, bg=COLORS["surface_2"])
        header.grid(row=0, column=0, columnspan=5, sticky="ew", padx=10, pady=(8, 2))
        header.columnconfigure(1, weight=1)
        enabled = tk.BooleanVar(value=bool(channel.get("enabled", True)))
        row_data["vars"]["enabled"] = (enabled, "bool")
        ttk.Checkbutton(header, text="启用", variable=enabled, style="Console.TCheckbutton").grid(row=0, column=0, sticky="w")
        self._label(header, str(channel.get("name") or channel.get("id") or "渠道"), fg=COLORS["text"], font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Button(header, text="删除", style="Danger.TButton", command=lambda item=row_data: self._delete_channel_row(item)).grid(row=0, column=2, sticky="e")
        if mode == "subscription":
            fields = (("id", "渠道 ID", "text"), ("model_mapping", "模型映射", "mapping"), ("supported_models", "支持模型", "csv"))
        else:
            fields = (("id", "渠道 ID", "text"), ("name", "名称", "text"), ("service_type", "协议", "select_gateway"), ("base_url", "Base URL", "text"), ("api_key_env", "API Key 环境变量", "text"), ("model", "本地模型", "text"), ("priority", "优先级", "number"), ("timeout_ms", "超时 ms", "number"), ("max_retries", "重试", "number"), ("health_path", "健康路径", "text"), ("model_mapping", "模型映射", "mapping"), ("supported_models", "支持模型", "csv"))
        for field_index, (name, label_text, field_type) in enumerate(fields):
            grid_row = 1 + field_index // 2
            label_col = (field_index % 2) * 2
            value_col = label_col + 1
            self._label(card, label_text, fg=COLORS["muted"], font=("Segoe UI", 8)).grid(row=grid_row, column=label_col, sticky="w", padx=(10, 6), pady=4)
            current = channel.get(name, "")
            if name == "api_key_env":
                keys = channel.get("api_keys")
                if isinstance(keys, list) and keys and isinstance(keys[0], Mapping):
                    current = keys[0].get("env", "")
            if name == "model_mapping":
                current = self._mapping_to_text(current)
            if name == "supported_models" and isinstance(current, (list, tuple)):
                current = ",".join(str(item) for item in current)
            if field_type == "select_gateway":
                variable = tk.StringVar(value=str(current))
                widget: tk.Widget = ttk.Combobox(card, textvariable=variable, state="readonly", values=("openai", "responses", "local"), style="Console.TCombobox")
                actual_type = "select"
            else:
                variable = tk.StringVar(value=str(current or ""))
                widget = ttk.Entry(card, textvariable=variable, style="Console.TEntry")
                actual_type = field_type
            widget.grid(row=grid_row, column=value_col, sticky="ew", padx=(0, 10), pady=3)
            row_data["vars"][name] = (variable, actual_type)
        self._channel_rows.append(row_data)

    @staticmethod
    def _mapping_to_text(value: Any) -> str:
        if not isinstance(value, Mapping):
            return str(value or "")
        return "; ".join(f"{key}={item}" for key, item in value.items())

    @staticmethod
    def _parse_mapping(value: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in str(value).replace("\n", ";").split(";"):
            if "=" not in part:
                continue
            key, mapped = part.split("=", 1)
            if key.strip() and mapped.strip():
                result[key.strip()] = mapped.strip()
        return result

    @staticmethod
    def _parse_number(value: Any) -> int | float:
        number = float(str(value).strip())
        return int(number) if number.is_integer() else number

    def _add_channel_row(self) -> None:
        if self.config_store:
            self._gateway_cache = add_gateway_channel(self._gateway_cache)
            self._gateway_cache_dirty = True
            self._show_page(self._page_key)

    def _delete_channel_row(self, row: Mapping[str, Any]) -> None:
        index = next((i for i, item in enumerate(self._channel_rows) if item is row), None)
        if index is not None:
            channels = list(self._gateway_cache.get("channels", []))
            if index < len(channels):
                self._gateway_cache["channels"] = [item for i, item in enumerate(channels) if i != index]
                self._gateway_cache_dirty = True
        self._show_page(self._page_key)

    def _collect_gateway_payload(self) -> dict[str, Any]:
        payload = copy.deepcopy(self._gateway_cache)
        for name, (variable, field_type, original) in self._gateway_scalar_vars.items():
            payload[name] = self._read_variable(variable, field_type, "gateway", name, original)
        channels: list[dict[str, Any]] = []
        for row in self._channel_rows:
            channel = copy.deepcopy(row["original"])
            for name, (variable, field_type) in row["vars"].items():
                if name == "model_mapping":
                    channel[name] = self._parse_mapping(str(variable.get()))
                elif name == "supported_models":
                    channel[name] = [item.strip() for item in str(variable.get()).split(",") if item.strip()]
                elif name == "api_key_env":
                    env_name = str(variable.get()).strip()
                    channel["api_keys"] = [{"env": env_name, "weight": 1}] if env_name else []
                else:
                    channel[name] = self._read_variable(variable, field_type, "gateway", name, channel.get(name))
            channels.append(channel)
        payload["channels"] = channels
        return payload

    # ---------- config save/reload ----------

    def _set_page_status(self, text: str, tone: str = "muted") -> None:
        self.var_page_status.set(text)
        if getattr(self, "page_status_label", None):
            colors = {"ok": COLORS["green"], "loading": COLORS["amber"], "error": COLORS["red"], "warn": COLORS["amber"]}
            self.page_status_label.configure(fg=colors.get(tone, COLORS["faint"]))

    def _reload_config_page(self) -> None:
        self._set_page_status("正在读取…", "loading")
        self._show_page(self._page_key)

    def _read_variable(self, variable: tk.Variable, field_type: str, module: str, name: str, original: Any = "") -> Any:
        value = variable.get()
        if field_type == "bool":
            return bool(value)
        if field_type == "number":
            try:
                return self._parse_number(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{module}.{name} 必须是数字") from exc
        if field_type == "secret":
            return preserve_secret(value, self._secret_original.get((module, name), original))
        return str(value).strip()

    def _collect_settings_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for (module, name), (variable, field_type, original) in self._form_vars.items():
            payload.setdefault(module, {})[name] = self._read_variable(variable, field_type, module, name, original)
        if self._provider_rows:
            providers: list[dict[str, Any]] = []
            for row in self._provider_rows:
                provider = copy.deepcopy(row["original"])
                for name, (variable, field_type) in row["vars"].items():
                    provider[name] = self._read_variable(variable, field_type, "llm", name, provider.get(name))
                providers.append(provider)
            payload.setdefault("llm", {})["providers"] = providers
        return payload

    def _save_current_page(self) -> None:
        if self._config_busy:
            self._set_page_status("正在保存，请稍候…", "warn")
            return
        if not self.config_store:
            self._set_page_status("未定位仓库", "error")
            return
        try:
            if self._page_key in {"channels", "subscriptions"}:
                payload: object = self._collect_gateway_payload()
                endpoint = "/api/settings/gateway"
            elif self._page_key == "gateway" or self._page_key == "cockpit":
                return
            else:
                payload = self._collect_settings_payload()
                endpoint = "/api/settings"
        except ConfigError as exc:
            self._set_page_status(f"校验失败：{exc}", "error")
            self._log("error", f"配置校验失败：{exc}")
            return
        self._config_busy = True
        self._set_page_status("正在保存…", "loading")
        if getattr(self, "page_save_button", None):
            self.page_save_button.configure(state=tk.DISABLED)
        threading.Thread(target=self._save_worker, args=(endpoint, payload), daemon=True, name="console-config-save").start()

    def _http_json(self, method: str, url: str, payload: object, timeout: float = 5.0) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            detail = raw or f"HTTP {exc.code}"
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, Mapping):
                    detail = str(parsed.get("detail") or parsed.get("error") or detail)
            except (ValueError, TypeError):
                pass
            raise ConfigError(f"HTTP 校验错误：{detail}") from exc
        if status >= 300:
            raise ConfigError(f"HTTP 校验错误：HTTP {status}")
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(f"控制面返回无效 JSON：{exc}") from exc
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    def _save_worker(self, endpoint: str, payload: object) -> None:
        assert self.config_store is not None
        try:
            try:
                base = self.config_store.control_api_base(self.config_store.read_settings())
                result = self._http_json("PUT", f"{base}{endpoint}", payload)
                if endpoint.endswith("/gateway"):
                    reload_info = result.get("reload") if isinstance(result, Mapping) else None
                    status = ("已保存 · 网关重载失败，请重启网关", "warn") if isinstance(reload_info, Mapping) and reload_info.get("ok") is False else ("已热应用 · 网关重载成功", "ok")
                else:
                    restart = result.get("requires_restart") if isinstance(result, Mapping) else []
                    status = ("已热应用 · 需重启：" + ", ".join(str(item) for item in restart[:4]), "warn") if isinstance(restart, list) and restart else ("已热应用", "ok")
                self._queue_event("config_status", status)
                self._queue_event("config_saved", payload)
            except urllib.error.URLError:
                path = self.config_store.save_gateway(payload) if endpoint.endswith("/gateway") else self.config_store.save_settings(payload)  # type: ignore[arg-type]
                self._queue_event("config_status", (f"已离线保存 · {path.name} · 下次启动生效", "warn"))
                self._queue_event("config_saved", payload)
            except (TimeoutError, ConnectionError, OSError):
                path = self.config_store.save_gateway(payload) if endpoint.endswith("/gateway") else self.config_store.save_settings(payload)  # type: ignore[arg-type]
                self._queue_event("config_status", (f"已离线保存 · {path.name} · 下次启动生效", "warn"))
                self._queue_event("config_saved", payload)
        except Exception as exc:  # noqa: BLE001 - worker reports visible UI error
            self._queue_event("config_error", str(exc))
        finally:
            self._queue_event("config_done", None)

    def _render_admin_tools(self, parent: tk.Frame, row: int) -> None:
        card = self._card(parent)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        self._label(card, "LOCAL OPERATIONS", fg=COLORS["cyan"], font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(13, 8))
        buttons = tk.Frame(card, bg=COLORS["surface"])
        buttons.pack(fill=tk.X, padx=16, pady=(0, 13))
        ttk.Button(buttons, text="重读全部配置", style="Console.TButton", command=self._reload_all_from_disk).pack(side=tk.LEFT)
        ttk.Button(buttons, text="打开配置目录", style="Ghost.TButton", command=self._open_config_dir).pack(side=tk.LEFT, padx=(7, 0))
        ttk.Button(buttons, text="查看系统日志", style="Ghost.TButton", command=lambda: (self._show_page("gateway"), self.root.after(100, self._open_log_viewer))).pack(side=tk.LEFT, padx=(7, 0))

    def _reload_all_from_disk(self) -> None:
        if not self.config_store:
            self._set_page_status("未定位仓库", "error")
            return
        self._set_page_status("正在重读…", "loading")
        try:
            self._settings_cache = self.config_store.read_settings()
            self._gateway_cache = self.config_store.read_gateway()
            self._settings_cache_dirty = False
            self._gateway_cache_dirty = False
            self._load_runtime_preferences()
            self._set_page_status("已重读磁盘配置", "ok")
            self._log("success", "已重读 settings.json 和 gateway/config.toml")
        except ConfigError as exc:
            self._set_page_status(f"重读失败：{exc}", "error")
            self._log("error", f"重读配置失败：{exc}")

    def _open_config_dir(self) -> None:
        folder = self.repo_root / "config" if self.repo_root else Path.home()
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer", str(folder)])
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"无法打开目录：{exc}")

    def _build_cockpit_page(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="nsew")
        self._label(card, "驾驶舱", fg=COLORS["cyan"], font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=28, pady=(28, 8))
        self._label(card, "驾驶舱继续使用现有 Web 控制台视觉，不在桌面 EXE 内嵌 WebView。", fg=COLORS["muted"], font=("Segoe UI", 10), wraplength=720, justify=tk.LEFT).pack(anchor="w", padx=28)
        self._label(card, "连接地址", fg=COLORS["faint"], font=("Segoe UI", 9)).pack(anchor="w", padx=28, pady=(24, 4))
        url = self._control_api_base()
        self._label(card, url, fg=COLORS["text"], font=("Consolas", 11)).pack(anchor="w", padx=28)
        ttk.Button(card, text="打开现有 Web 控制台", style="Primary.TButton", command=lambda: webbrowser.open(url)).pack(anchor="w", padx=28, pady=(20, 28))

    def _control_api_base(self) -> str:
        if self.config_store:
            try:
                return self.config_store.control_api_base(self.config_store.read_settings())
            except ConfigError:
                pass
        return f"http://127.0.0.1:{DEFAULT_CONTROL_API_PORT}"

    # ---------- commands and repository ----------

    def _load_repo_root(self) -> Path | None:
        saved: Path | None = None
        try:
            if CONFIG_FILE.is_file():
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                saved_value = data.get("repo_root")
                if saved_value:
                    saved = Path(str(saved_value))
        except (OSError, ValueError, TypeError):
            saved = None
        return find_repo_root() or (saved if saved and (saved / LIFECYCLE_SCRIPT).is_file() else None)

    def _save_repo_root(self) -> None:
        if self.repo_root is None:
            return
        try:
            CONFIG_FILE.write_text(json.dumps({"repo_root": str(self.repo_root)}, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self._log_console(logging.WARNING, f"保存仓库目录失败: {exc}")

    def _update_repo_label(self) -> None:
        self.var_settings_path.set(str(self.repo_root / "config" / "settings.json") if self.repo_root else "—")
        self.var_gateway_config_path.set(str(self.repo_root / "gateway" / "config.toml") if self.repo_root else "—")

    def _choose_repo(self) -> None:
        chosen = filedialog.askdirectory(title="选择 qmt 仓库根目录（含 scripts\\service_control.ps1）")
        if not chosen:
            return
        candidate = Path(chosen).resolve()
        if not (candidate / LIFECYCLE_SCRIPT).is_file():
            messagebox.showerror(APP_TITLE, f"该目录下未找到 {LIFECYCLE_SCRIPT}")
            return
        self.repo_root = candidate
        self.config_store = ConfigStore(candidate)
        self.controller.set_repo_root(candidate)
        self._save_repo_root()
        self._setup_console_logger(candidate)
        self._update_repo_label()
        self._log("success", f"已设置仓库目录: {candidate}")
        self._load_runtime_preferences()
        self._show_page(self._page_key)
        self._refresh_once()

    def _save_operator_preferences(self) -> None:
        if not self.repo_root or self._config_busy:
            return
        payload = {"profile": {"expert_mode": bool(self.var_expert_mode.get())}, "trading": {"virtual_trading": bool(self.var_virtual_trading.get())}}
        self._config_busy = True
        threading.Thread(target=self._save_worker, args=("/api/settings", payload), daemon=True, name="console-preferences").start()

    def _ensure_repo(self) -> bool:
        if self.repo_root is not None:
            return True
        messagebox.showwarning(APP_TITLE, "请先在管理面板选择仓库目录")
        return False

    def _busy_message(self) -> bool:
        if self._busy:
            self._log("warn", f"系统正在{self._busy_label}，请等待完成后再操作…")
            return True
        return False

    def _set_busy(self, action: str) -> None:
        self._busy = True
        self._busy_label = action
        for name in ("btn_start", "btn_stop", "btn_restart", "btn_model_start", "btn_model_stop"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=tk.DISABLED)
        self.var_status.set(f"正在{action}")
        self.var_status_detail.set("统一服务控制器执行中")
        self._set_status_color(COLORS["amber"])

    def _release_busy(self) -> None:
        self._busy = False
        self._busy_label = ""
        for name in ("btn_start", "btn_stop", "btn_restart", "btn_model_start", "btn_model_stop"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=tk.NORMAL)

    def _start(self) -> None:
        if not self._ensure_repo() or self._busy_message():
            return
        if self.controller.start(self.var_full_build.get()):
            self.var_last_action.set("启动全部命令已发送")

    def _stop(self) -> None:
        if not self._ensure_repo() or self._busy_message():
            return
        if self.controller.stop():
            self.var_last_action.set("停止全部命令已发送")

    def _restart(self) -> None:
        if not self._ensure_repo() or self._busy_message():
            return
        if self.controller.restart(self.var_full_build.get()):
            self.var_last_action.set("重启全部命令已发送")

    def _start_local_model(self) -> None:
        if not self._ensure_repo() or self._busy_message():
            return
        if self.controller.start_local_model():
            self.var_last_action.set("本地模型启动命令已发送")

    def _stop_local_model(self) -> None:
        if not self._ensure_repo() or self._busy_message():
            return
        if self.controller.stop_local_model():
            self.var_last_action.set("本地模型停止命令已发送")

    # ---------- health and event pump ----------

    def _queue_event(self, kind: str, value: object) -> None:
        self._output_queue.put((kind, value))

    def _schedule_poll(self, delay: float) -> None:
        try:
            self.root.after(max(0, int(delay * 1000)), self._poll_health)
        except tk.TclError:
            pass

    def _poll_health(self) -> None:
        self._refresh_once()
        self._schedule_poll(float(self._poll_seconds.get()))

    def _change_poll(self, _event: object = None) -> None:
        try:
            self._poll_seconds.set(float(self.poll_combo.get()))
        except (ValueError, tk.TclError):
            self._poll_seconds.set(STATUS_POLL_SECONDS)

    def _refresh_once(self) -> None:
        if self.controller.refresh_health():
            self.var_status_detail.set("正在读取控制面和网关状态…")
            self._set_status_color(COLORS["amber"])

    @staticmethod
    def _display_number(value: Any) -> str:
        return "—" if value is None or value == "" else str(value)

    @staticmethod
    def _format_uptime(value: Any) -> str:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return "—"
        seconds = int(value)
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        if days:
            return f"{days}d {hours:02d}h"
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"

    def _apply_health(self, snapshot: HealthSnapshot) -> None:
        self.var_last_check.set(f"最后检查 {snapshot.checked_at.astimezone().strftime('%H:%M:%S')}")
        self.var_mode.set("模拟盘" if snapshot.mode == "paper" else ("实盘" if snapshot.mode == "live" else snapshot.mode.upper()))
        values = {"api": self.var_api, "gateway": self.var_gateway, "local_model": self.var_local_model, "quote_bridge": self.var_quote_bridge, "news_bridge": self.var_news_bridge}
        for key, variable in values.items():
            ok = snapshot.is_ok(key)
            variable.set("在线" if ok else "离线")
            card = self._status_cards.get(key)
            if card:
                value_label, detail_label, dot = card
                value_label.configure(fg=COLORS["green"] if ok else COLORS["red"])
                dot.configure(fg=COLORS["green"] if ok else COLORS["red"])
                payload = snapshot.payload(key)
                detail_label.configure(text=(str(payload.get("status", "ready")) if isinstance(payload, Mapping) else "未响应").upper())
        gateway = snapshot.gateway_status if isinstance(snapshot.gateway_status, Mapping) else {}
        self.var_gateway_port.set(self._display_number(gateway.get("port")))
        self.var_gateway_uptime.set(self._format_uptime(gateway.get("uptime_seconds")))
        healthy = gateway.get("channels_healthy")
        total = gateway.get("channels_total")
        self.var_gateway_channels.set(f"{healthy} / {total}" if healthy is not None and total is not None else "—")
        self.var_gateway_version.set(self._display_number(gateway.get("version")))
        self.var_gateway_pid.set(f"PID  {self._display_number(gateway.get('pid'))}")
        self.var_binary_path.set(self._display_number(gateway.get("binary_path")))
        self.var_data_dir.set(self._display_number(gateway.get("data_dir")))
        gateway_error = str(gateway.get("error") or "").strip()
        if gateway_error:
            # Health polling is periodic; report a gateway failure once per
            # state transition instead of filling the live log every cycle.
            if gateway_error != self._last_gateway_error:
                self._log("warn", f"网关状态：{gateway_error}")
            self._last_gateway_error = gateway_error
        elif self._last_gateway_error:
            self._log("success", "网关状态已恢复")
            self._last_gateway_error = None
        if snapshot.all_core_ok and bool(gateway.get("reachable")):
            self.var_status.set("网关在线")
            self.var_status_detail.set("控制面与核心服务健康 · 可打开 Web 控制台")
            self._set_status_color(COLORS["green"])
        elif snapshot.running or bool(gateway.get("reachable")):
            self.var_status.set("部分在线")
            self.var_status_detail.set("核心服务未完全就绪，请查看实时日志")
            self._set_status_color(COLORS["amber"])
        else:
            self.var_status.set("已停止")
            self.var_status_detail.set("网关不可达 · 查看实时日志" if gateway_error else "未检测到运行中的核心服务")
            self._set_status_color(COLORS["faint"])

    def _set_status_color(self, color: str) -> None:
        if getattr(self, "status_label", None):
            self.status_label.configure(fg=color)
        if getattr(self, "sidebar_status_dot", None):
            self.sidebar_status_dot.configure(fg=color)

    def _pump_queue(self) -> None:
        try:
            processed = 0
            while processed < PUMP_MAX_PER_TICK:
                kind, value = self._output_queue.get_nowait()
                if kind == "log":
                    self._log("info", str(value))
                elif kind == "error":
                    self._log("error", str(value))
                elif kind == "warn":
                    self._log("warn", str(value))
                elif kind == "success":
                    self._log("success", str(value))
                elif kind == "busy":
                    self._set_busy(str(value))
                elif kind == "health" and isinstance(value, HealthSnapshot):
                    self._apply_health(value)
                elif kind == "done":
                    self._release_busy()
                    if isinstance(value, OperationResult):
                        self.var_last_action.set(f"{value.action}完成 · {value.elapsed_seconds:.1f}s")
                    self._refresh_once()
                elif kind == "config_status" and isinstance(value, tuple):
                    self._set_page_status(str(value[0]), str(value[1]))
                elif kind == "config_saved":
                    if self._page_key in {"channels", "subscriptions"}:
                        self._gateway_cache_dirty = False
                    else:
                        self._settings_cache_dirty = False
                    self._log("success", "配置保存结果已同步")
                elif kind == "config_error":
                    self._set_page_status(f"保存失败：{value}", "error")
                    self._log("error", f"配置保存失败：{value}")
                elif kind == "config_done":
                    self._config_busy = False
                    if getattr(self, "page_save_button", None):
                        self.page_save_button.configure(state=tk.NORMAL)
                processed += 1
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - the pump must always reschedule
            self._log("error", f"内部错误（队列处理）: {exc}")
            self._log_console(logging.ERROR, f"队列处理异常: {exc}")
        finally:
            try:
                self.root.after(80, self._pump_queue)
            except tk.TclError:
                pass

    # ---------- compatibility helper for test_run_script.py ----------

    def _run_script(self, script: Path, args: list[str], label: str) -> None:
        """Legacy test hook; production operations use StackController."""

        def worker() -> None:
            reader: threading.Thread | None = None
            stop = threading.Event()
            log_path: str | None = None
            try:
                shell = getattr(self, "_shell", None) or shell_command()
                self._shell = shell
                fd, log_path = tempfile.mkstemp(prefix="console_script_", suffix=".log")
                os.close(fd)
                with open(log_path, "wb") as out:
                    proc = subprocess.Popen([*shell, "-File", str(script), *args], cwd=str(self.repo_root), stdout=out, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                reader = threading.Thread(target=self._tail_script_output, args=(log_path, stop), daemon=True)
                reader.start()
                code = proc.wait()
                self._output_queue.put(("log" if code == 0 else "error", f">>> {label}脚本执行完成 (exit {code})"))
            except Exception as exc:  # noqa: BLE001
                self._output_queue.put(("error", f">>> 执行异常: {exc}"))
            finally:
                stop.set()
                if reader is not None:
                    reader.join(timeout=2.0)
                if log_path:
                    try:
                        os.remove(log_path)
                    except OSError:
                        pass
                self._output_queue.put(("done", label))

        threading.Thread(target=worker, daemon=True).start()

    def _tail_script_output(self, path: str, stop: threading.Event) -> None:
        tail_file(path, stop, lambda kind, value: self._output_queue.put((kind, value)))

    def _on_close(self) -> None:
        self._log_console(logging.INFO, "控制台退出")
        self.controller.close()
        if getattr(self, "log", None):
            self._set_activity_log_view()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.1)
    except tk.TclError:
        pass
    TraderConsole(root)
    root.mainloop()


if __name__ == "__main__":
    main()
