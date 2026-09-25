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
租约和内存回收，消费研究、回测、Trade Plan、个人档案、退出建议、Jev 训练和 System-2 队列。

## 管理器

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

## 生命周期

```powershell
.\scripts\native\ashare-native.cmd doctor
.\scripts\native\ashare-native.cmd status -Json
.\scripts\native\ashare-native.cmd stop
.\scripts\native\ashare-native.cmd start
```

状态、日志、数据库、对象、lake 和 SPA 资源都位于 `<runtime>`。下载归档、生成配置、凭据、数据库
和日志不属于仓库产物。前端验证使用 `cd web; npm test -- --run; npm run build`，构建产物不提交。
