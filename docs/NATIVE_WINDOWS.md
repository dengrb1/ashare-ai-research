# Windows Native Runtime

Windows 原生入口把运行时文件放在仓库外的 `runtime` 目录，不启动 Docker 或 WSL。可用
`ASHARE_NATIVE_ROOT` 或 `-Root` 指定目录。

## 安装和运行

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\native\ashare-native.cmd install
.\scripts\native\ashare-native.cmd start
.\scripts\native\ashare-native.cmd status
```

安装器校验 PostgreSQL、Redis-compatible、Python 依赖并构建外部 Web 资源。管理员凭据只写入
`<runtime>\config\admin-credentials.txt`；模型 API Key 通过设置页进入加密数据库。运行组包含
PostgreSQL、Redis、API、行情桥、模型 `gateway` 和单一 `job-worker`，不包含搜索或 Edge Gateway。

原生 API 与 SPA 同源提供页面，所有研究结果和 API 契约与 Docker 相同。Worker 使用隔离子进程、队列
租约和内存回收，消费研究、回测、Trade Plan、个人档案、退出建议、因子进化和 System-2 队列。

## 管理器

推荐通过开发辅助脚本生成可安装的 Windows 应用包：

```powershell
scripts\dev.cmd all          # Web、托盘管理器、安装程序和开发控制台
scripts\dev.cmd installer    # 只生成安装程序
```

运行 `windows\native-control-center\dist\AshareAI-Setup.exe` 后，开始菜单会创建
`AshareAI\AshareAI 本机运行管理器` 和安装目录入口。安装器默认注册当前用户开机启动，管理器
启动后进入托盘；关闭窗口会隐藏到托盘，托盘菜单提供打开 Web、启动/停止服务、显示控制台和退出。
管理器中的“开机启动”开关可以随时修改，卸载时会移除开始菜单、桌面和启动项。

```powershell
.\windows\native-control-center\build.ps1
.\windows\native-control-center\AshareAI.NativeControlCenter.exe
```

管理器支持安装/更新、启动、停止、重启、修复、诊断、打开 Web、状态刷新和日志查看。CLI 支持：

```powershell
.\windows\native-control-center\AshareAI.NativeControlCenter.Cli.exe status --json
.\windows\native-control-center\AshareAI.NativeControlCenter.Cli.exe start
.\windows\native-control-center\AshareAI.NativeControlCenter.Cli.exe logs --tail 200
```

## 源码开发模式

测试最新源码时，不需要重新生成或安装桌面安装包。双击 `tools\console\run-console.vbs` 打开开发工作台；首次打开时可以使用仓库 `.venv` 中的 Python，若没有则使用系统 `pythonw.exe`。在“开发工作台”页面选择源码仓库和源码之外的 runtime（默认 `%LOCALAPPDATA%\AshareAI\dev-runtime`），再按顺序点击“初始化 / 更新 runtime”和“启动源码服务”。

开发模式复用安装版的 PostgreSQL、Redis、依赖、配置和后台进程生命周期，但 API 从源码目录的 `src` 加载 Python 代码，页面从 `web\dist` 加载。Python 改动后点击“重启服务”；Web 改动后点击“构建 Web”，再重启服务并刷新浏览器。开发启动时会同步源码中的迁移与版本化配置，并应用尚未执行的迁移。工作台也提供停止、诊断、打开 Web、Python 测试、Web 测试和实时日志；构建区域可编译托盘管理器、安装包和开发控制台，也可一键全量构建，不需要通过命令行操作。

开发控制台本身需要分发给其他机器时，仍可通过 `scripts\dev.cmd console` 编译为独立 exe；日常源码调试直接双击 VBS 启动器即可。

## 生命周期

```powershell
.\scripts\native\ashare-native.cmd doctor
.\scripts\native\ashare-native.cmd status -Json
.\scripts\native\ashare-native.cmd stop
.\scripts\native\ashare-native.cmd start
```

状态、日志、数据库、对象、lake 和 SPA 资源都位于 `<runtime>`。下载归档、生成配置、凭据、数据库
和日志不属于仓库产物。前端验证使用 `cd web; npm test -- --run; npm run build`，构建产物不提交。
