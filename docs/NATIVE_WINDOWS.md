# Windows Native Runtime

The repository includes a native Windows entry point that keeps every runtime
file outside the checkout. It does not start Docker or WSL. The default runtime
root is a `runtime` folder beside the installed manager; when launched from the
source checkout it falls back to `%LOCALAPPDATA%\AshareAI\runtime`. Override it with
`ASHARE_NATIVE_ROOT` or `-Root`, but keep it outside the source directory.

## Install and Run

Run PowerShell from the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\native\ashare-native.cmd install
.\scripts\native\ashare-native.cmd start
.\scripts\native\ashare-native.cmd status
```

The installer validates the locked PostgreSQL and Redis-compatible archives
before extraction, checks out the pinned SearXNG source commit, creates the
external Python environment, and builds `web/` into the external runtime root.
The checksum source for an archive is either the lock entry, the vendor
checksum URL, or the fixed GitHub release asset digest. A missing trusted
checksum is an installation error; the installer never silently accepts an
unverified archive.

The generated administrator credentials are written only to
`<runtime>\config\admin-credentials.txt`. Model credentials are entered later
through the existing administrator model-settings page and are stored by the
same encrypted database path as the Docker deployment.

Installation creates the local non-administrator `AshareAIService` account and
runs PostgreSQL, Redis, SearXNG, API, and workers with that identity. The
installer grants it access only to the external runtime and the host Python
installation used to create the venv. Port selection is host-aware: the chosen
PostgreSQL, Redis, API, and SearXNG ports are persisted in
`<runtime>\config\native-ports.json` and reused on every restart.

The native API serves the built SPA from the same origin, so browser Cookie,
CSRF, SSE, queue, search, cache, and model-settings behavior remains unchanged.
The `web` status entry is explicitly marked `embedded_in=api`; this avoids a
second static server and preserves same-origin requests without adding a
reverse-proxy process.

## Windows 本机运行管理器

Windows 安装包包含编译后的 WinForms 本机运行管理器。当前为测试版本，可能不稳定；构建和启动：

```powershell
.\windows\native-control-center\build.ps1
.\windows\native-control-center\AshareAI.NativeControlCenter.exe
.\windows\native-control-center\dist\AshareAI-Setup.exe
```

管理器会请求管理员权限，因为安装依赖、服务账户管理和看门狗任务注册需要该权限。看门狗以
受限的 `AshareAIService` 本地账户运行，不使用 `SYSTEM`，并且运行目录会收紧为当前管理员、
该服务账户、SYSTEM 和本机 Administrators 可访问。
GUI 显示运行健康状态、服务 PID、内存、端口、看门狗状态和最近日志。简化后的窗口支持
安装/更新、启动、停止、重启、修复、诊断、打开 Web UI 和刷新状态，也可以配置研究模式、
Worker 数、看门狗间隔、自动刷新和外部运行目录。

同一个管理器也可以从命令提示符或 PowerShell 调用，无需打开 GUI：

```powershell
.\windows\native-control-center\AshareAI.NativeControlCenter.Cli.exe status --json
.\windows\native-control-center\AshareAI.NativeControlCenter.Cli.exe start --research-mode DUAL --research-workers 2
.\windows\native-control-center\ashareai.cmd logs --tail 200
```

安装后的 `ashareai.cmd` 位于管理器 EXE 同目录，并转发到
`AshareAI.NativeControlCenter.Cli.exe`。支持命令为 `install`、`start`、
`stop`、`restart`、`repair`、`status`、`doctor`、`open` 和 `logs`。

GUI 偏好存储在 `<runtime>\config\native-gui.json`，该文件不保存密钥。EXE 内嵌
`ashare-native.ps1` 和版本化依赖锁，启动时提取到本地管理目录，因此安装后的管理器
不依赖仓库脚本。单文件安装包会创建快捷方式并自动启动依赖安装；它包含固定 Python、
固定 SearXNG 载荷和预构建 Web UI，目标机器不需要 Node.js 或 Git。

安装包支持无人值守部署：

```powershell
.\windows\native-control-center\dist\AshareAI-Setup.exe /quiet /dir "D:\AshareAI" /root "D:\AshareAI\runtime"
.\windows\native-control-center\dist\AshareAI-Setup.exe /quiet /start-services
.\windows\native-control-center\dist\AshareAI-Setup.exe /quiet /no-install-deps
.\windows\native-control-center\dist\AshareAI-Setup.exe /uninstall /quiet
```

静默安装默认安装依赖，并写日志到 `%TEMP%\AshareAI-Setup.log`。`/start-services`
会在依赖安装完成后启动本机运行组；`/no-install-deps` 只铺设文件、快捷方式和卸载入口。

## Linux 本机运行管理器

仓库中也有独立的 Linux 非 Docker 管理器目录。当前为测试版本，可能不稳定：

```text
linux/native-control-center/
```

该目录包含 Tkinter/ttk GUI 和同目录 Linux 控制器。Linux GUI 对齐 Windows
管理器的功能面：运行目录、安装/更新、启动、停止、重启、修复、诊断、打开 Web、状态刷新、
服务表、活动记录和看门狗日志。它不启动 Docker。

在 Linux 上运行当前原型：

```bash
python3 linux/native-control-center/native_control_center.py
```

控制器返回与 Windows 管理器兼容的 `status --json` 和 `doctor --json` 结构，状态命令
使用快速进程快照，不会因为未安装而抛出异常。`install` 会在运行目录创建私有 venv、
安装 `requirements.runtime.lock`、复制 `web/dist` 并记录依赖路径；启动和停止由控制器
维护进程状态、日志和端口配置。PostgreSQL、Redis-compatible 和 Python 的系统条件缺失
时，安装会返回明确诊断，避免界面出现无原因的灰色按钮。

## Lifecycle

```powershell
.\scripts\native\ashare-native.cmd doctor
.\scripts\native\ashare-native.cmd status -Json
.\scripts\native\ashare-native.cmd stop
.\scripts\native\ashare-native.cmd start -ResearchMode DUAL -ResearchWorkers 2
```

`status` reads only the PIDs recorded by the native entry point. Its
`NATIVE_PROCESS_GROUP` total is the sum of distinct Windows `WorkingSet64`
values for the managed process trees, which is the native equivalent of the
Docker cgroup working-set measurement. Logs are in `<runtime>\logs`; PostgreSQL,
Redis, objects, lake files, the SearXNG checkout, Python environment and SPA
assets are all under `<runtime>`.

The native lock is intentionally kept in Git at
`scripts/native/dependencies.lock.json`. Downloaded archives, generated
configuration, credentials, databases, logs and build output are not repository
artifacts.
