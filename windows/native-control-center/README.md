# AshareAI Windows 本机运行管理器

直接双击安装包时会先显示目录选择窗口。默认管理器目录是安装包所在目录下的
AshareAI，默认运行目录是该目录下的 runtime；两个目录都可以单独浏览选择。
管理器从源码目录启动时会自动回退到用户本地的 AshareAI/runtime，避免污染源码树。

本目录存放 Windows 原生管理器和单文件安装包构建脚本。管理器使用系统自带
.NET Framework WinForms 编译，不需要安装 .NET SDK。安装后的运行方式不依赖
Docker 或 WSL。

## 构建

在仓库根目录运行：

```powershell
.\windows\native-control-center\build.ps1
```

构建产物：

- `AshareAI.NativeControlCenter.exe`：中文 GUI 管理器。
- `AshareAI.NativeControlCenter.Cli.exe`：命令行管理器。
- `ashareai.cmd`：命令提示符/PowerShell 友好的 CLI 包装器。
- `dist\AshareAI-Setup.exe`：单文件 Windows 安装包。

GitHub Actions 会在每次分支推送和 `main` Pull Request 上构建同一安装包，并同时
上传 `AshareAI-Setup.exe.sha256`。安装前应校验 SHA-256 与同一工作流产物一致。

## GUI 与命令行

GUI 入口：

```powershell
.\windows\native-control-center\AshareAI.NativeControlCenter.exe
```

命令行入口：

```powershell
.\windows\native-control-center\AshareAI.NativeControlCenter.Cli.exe status --json
.\windows\native-control-center\ashareai.cmd start --research-mode DUAL --research-workers 2
.\windows\native-control-center\ashareai.cmd logs --tail 200
```

CLI 支持 `install`、`start`、`stop`、`restart`、`repair`、`status`、
`doctor`、`open` 和 `logs`。可选参数包括 `--root <运行目录>`、
`--source-root <应用载荷目录>`、`--research-mode SERIAL|DUAL`、
`--research-workers 0..2`、`--watchdog-interval <秒>`、`--json` 和
`--tail <行数>`。

## 安装包

安装包会携带 Python 应用、预构建 Web UI、固定版本 Python 安装器和固定提交的
SearXNG 压缩包。首次安装时，管理器会校验、下载或展开 PostgreSQL、
Redis-compatible、Python、SearXNG 和 Python 包依赖。目标机器不需要 Node.js、
Git、Docker Desktop 或 WSL。

安装器支持无人值守部署：

```powershell
.\windows\native-control-center\dist\AshareAI-Setup.exe /quiet /dir "D:\AshareAI" /root "D:\AshareAI\runtime"
.\windows\native-control-center\dist\AshareAI-Setup.exe /quiet /start-services
.\windows\native-control-center\dist\AshareAI-Setup.exe /quiet /no-install-deps
.\windows\native-control-center\dist\AshareAI-Setup.exe /uninstall /quiet
```

静默安装默认会安装依赖，并写日志到 `%TEMP%\AshareAI-Setup.log`。
`/start-services` 会在依赖安装完成后启动本机运行组；`/no-install-deps` 只铺设文件、
快捷方式和卸载入口。

## 内置脚本

管理器会把 `ashare-native.ps1` 和 `dependencies.lock.json` 作为资源嵌入 EXE，
启动时提取到用户本地管理目录。因此安装后的管理器不依赖仓库中的松散脚本文件。
长时间操作只显示在窗口底部状态栏和活动记录中，不会把鼠标全局设置为忙碌光标。

Edge Gateway 与 Docker/Linux 原生版保持同一套管理能力和 API 契约。Windows 原生运行目录中的 `config\edge-gateway` 保存 `frpc.toml` 与 `managed.conf`；管理页面会读取文件外部修改，保存和控制器应用也会写回同一目录。
