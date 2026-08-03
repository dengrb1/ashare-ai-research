# AshareAI Linux 本机运行管理器

默认运行目录是管理器目录下的 runtime 文件夹；从源码目录启动时为避免污染源码树，
会自动回退到用户目录下的 .local/share/ashare-ai/runtime。GUI 中可以使用“浏览”选择其他目录。

本目录存放 Linux 非 Docker 版管理器、Tkinter/ttk GUI 和同目录控制器。当前为测试版本，可能不稳定，
不建议直接用于生产环境。
目标是与 Windows 本机管理器保持同一组核心能力，同时把运行目录、依赖、状态和日志
全部放在源码树外。

当前目录是独立管理器目录：

```text
linux/native-control-center/
```

## 当前内容

- `native_control_center.py`：标准库 Tkinter/ttk GUI 原型。
- `ashare-native-linux.sh`：Shell 入口，转发到 Linux 控制器并保留命令契约。
- `native_controller.py`：标准库控制器，负责安装、进程组、端口、状态和日志。
- `README.md`：Linux 非 Docker 管理器说明。

GUI 已提供与 Windows 管理器相同的用户侧入口：安装更新、启动、停止、重启、修复、
诊断、打开 Web、刷新状态、运行目录、研究模式、研究进程、看门狗间隔、服务表、
活动记录和看门狗日志。

## 运行方式

在 Linux 上运行：

```bash
python3 linux/native-control-center/native_control_center.py
```

也可以显式指定控制器、源码目录和运行目录：

```bash
python3 linux/native-control-center/native_control_center.py \
  --controller linux/native-control-center/ashare-native-linux.sh \
  --source-root /opt/ashare-ai/app \
  --root "$HOME/.local/share/ashare-ai/runtime"
```

默认运行目录为：

```text
~/.local/share/ashare-ai/runtime
```

## 控制器契约

Linux 控制器应暴露与 Windows CLI 相同的命令面：

```bash
ashare-native-linux.sh install --root "$HOME/.local/share/ashare-ai/runtime" --source-root /opt/ashare-ai/app
ashare-native-linux.sh start --root "$HOME/.local/share/ashare-ai/runtime"
ashare-native-linux.sh status --json
ashare-native-linux.sh stop
ashare-native-linux.sh restart
ashare-native-linux.sh repair
ashare-native-linux.sh doctor --json
```

`status --json` 返回字段与 Windows 管理器一致：`desired_state`、
`runtime_healthy`、`ports`、`watchdog`、`watchdog_task`、
`total_working_set_mib` 和 `services`。

## 目标运行结构

- 运行目录：默认 `~/.local/share/ashare-ai/runtime`。
- 配置目录：`<runtime>/config`。
- 状态目录：`<runtime>/state`。
- 日志目录：`<runtime>/logs`。
- 系统包应用载荷：`/opt/ashare-ai/app`。
- 便携包应用载荷：随 AppImage 或便携目录放置。
- 不启动 Docker、Docker Compose 或 WSL。

## 依赖与运行限制

安装会创建私有 venv 并安装 `requirements.runtime.lock`。Linux 主机还必须提供：

- PostgreSQL Linux 二进制包或预构建包产物。
- Valkey 或 Redis-compatible Linux 二进制包。
- 固定提交的 SearXNG 源码压缩包。
- Python 运行时策略：
  - DEB/RPM 包使用系统 Python 3.11 或 3.12 创建私有 venv。
  - AppImage/便携包可携带可重定位 CPython 和私有 venv。
- 预构建 `web/dist` 复制到 `<runtime>/web`。

安装完成后会把 PostgreSQL、Redis、Python、API 和 Web 资源的绝对路径写入
`<runtime>/config/native-paths.json`。缺少系统依赖时会列出缺少的命令或 Python 包，
不会把未完成目录报告为可启动状态。

状态命令只读取状态文件和进程快照；GUI 的状态刷新不会锁定安装、启动、停止等操作。
执行中的实际管理命令才会暂时锁定同组按钮，避免重复提交。

GUI 不承载服务语义，只调用控制器并渲染状态、日志和命令输出。
