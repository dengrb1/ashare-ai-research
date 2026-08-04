# A 股 AI 自动投研系统 v2.0.3

每日收盘后运行的全 A 动态股票池研究、评分、候选过滤、模拟组合与事件驱动回测系统。首版不连接自动实盘下单。

## 核心保证

- 所有研究数据统一使用 `symbol + trading_date + available_at`，任何特征、评分和回测都必须显式给出 `decision_at`，并拒绝 `available_at > decision_at` 的未来信息。
- 原始载荷、Parquet 快照、模型产物和报告均记录来源、抓取时间、版本与 SHA-256；分析只读取已提交的不可变快照。
- 基本面、技术面、新闻情绪 Agent 只输出经过 Pydantic 严格校验的子分和证据。Manager 只能归纳结论。
- 综合分由版本化纯函数确定：基本面 35%、技术面 35%、事件/情绪 20%、数据质量与置信度 10%。
- 涨跌停、T+1、申报单位、费用和新股特殊期由带生效日期的规则数据匹配；安全关键规则缺失或冲突时禁止交易。
- 每日目标为 15 只模拟持仓，执行单股 8%、单行业 25%、单日单边换手 20% 和等风险约束。最大回撤熔断时只发布观察报告。

## 分层结构

```text
src/ashare_ai/
  core/            核心契约、哈希、时点可用性
  adapters/        AKShare、Tushare、东方财富及交易所适配器
  ingestion/       标准化、复权、披露校验和快照接入
  features/        基本面、技术面、情绪和质量特征
  agents/          结构化 Agent 与 Manager 边界
  scoring/         确定性质量分和综合评分
  universe/        动态可交易池
  quant/           Qlib 数据集、滚动验证和预测分位
  trading/         数据驱动 A 股规则、成交、费用与 T+1
  portfolio/       等风险组合、约束、事件风险和熔断
  backtest/        事件驱动回测、基准、指标和复现哈希
  storage/         PostgreSQL、Parquet/DuckDB、对象存储
  reports/         可追溯日报
  orchestration/   Prefect 收盘后任务流
  api/             FastAPI 查询和回测任务 API
```

PostgreSQL 保存控制面、规则、运行、评分、组合和审计；Parquet 保存不可变数据快照与分析明细；DuckDB 只查询显式提交的 Manifest；本地内容寻址对象卷或外部 S3 保存公告原文、模型和报告。

## 本地开发

需要 Python 3.11、Node.js 20+。本地进程读取 `.env`；首次配置可从宿主机模板复制，随后只在未跟踪的 `.env` 中填写管理员和 LLM 凭据：

```powershell
Copy-Item .env.local.example .env
python -m venv .venv
.\.venv\Scripts\python -m pip install ".[dev]"
```

不要把真实密码、Token 或 API Key 写回任何 `*.example` 文件。仓库中的本地模板使用宿主机地址：PostgreSQL `127.0.0.1:5432`、Redis `127.0.0.1:6379`；Docker 模板改用 `postgres`、`redis` 服务名和 `/data` 容器路径。

### 本地完整链路

按以下顺序启动。若依赖服务由 Docker 提供，只启动基础依赖，不在容器里启动 API/Worker：

```powershell
Copy-Item .env.docker.example .env.docker
docker compose up -d postgres redis
```

基础服务健康后执行只读诊断和迁移：

```powershell
.\.venv\Scripts\ashare-ai doctor
.\.venv\Scripts\ashare-ai migrate
```

诊断会检查配置文件、数据库、Redis、对象存储、三类工厂、Worker 模块和行情连通性，并只显示通过/警告/失败原因，不输出密码或 Token。网络受限时可用 `ashare-ai doctor --skip-market` 跳过外部行情请求，但这不等同于完成行情验收。

然后分别打开终端启动 API、Research Worker、Backtest Worker；需要工作日 18:00 自动调度时再启动 Prefect 调度进程：

```powershell
.\.venv\Scripts\ashare-ai api
```

```powershell
.\.venv\Scripts\python -m ashare_ai.orchestration.research_worker
```

```powershell
.\.venv\Scripts\python -m ashare_ai.orchestration.backtest_worker
```

```powershell
.\.venv\Scripts\python -m ashare_ai.orchestration.runner
```

首次启动前请设置管理员账号；系统没有公开注册入口：

```powershell
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD = "请替换为至少14位的强密码"
```

管理员通过 WebGUI 创建、禁用和重置其他账号；新建和重置密码至少 12 位，生产启动管理员
密码至少 14 位。认证使用服务端数据库会话、
HttpOnly Cookie、双提交 CSRF 校验和 Argon2 密码哈希；禁用账号或重置密码会立即
撤销该用户现有会话。生产环境必须设置 `COOKIE_SECURE=true`、显式 `TRUSTED_HOSTS`，
并通过 HTTPS 暴露站点；不满足生产安全约束时 API 会拒绝启动。

原生手机客户端使用短期 Bearer 访问令牌和强制轮换的刷新令牌：访问令牌默认 15 分钟，
刷新令牌默认 30 天，可分别用 `ACCESS_TOKEN_TTL_MINUTES` 和
`REFRESH_TOKEN_TTL_DAYS` 调整。数据库只保存 SHA-256 哈希，不保存令牌明文。Bearer
写请求不执行浏览器 CSRF 校验；Cookie 写请求仍必须提供双提交 CSRF。生产手机客户端必须
通过 HTTPS 调用明确配置的 API 地址，本服务不开放通配 CORS。

前端位于 `web/`，开发环境由 Vite 把 `/api` 代理到本地 FastAPI：

```powershell
cd web
npm ci
npm run dev
```

访问 `http://localhost:5173` 登录。主题支持“跟随系统 / 浅色 / 深色”三态并按此顺序循环；没有保存偏好时默认跟随系统，系统配色变化会实时生效。选择保存在 `localStorage`，已有的 `ashare-theme=light/dark` 会继续作为显式覆盖；页面首屏脚本会在 React 启动前同步实际渲染主题和浏览器 `theme-color`，避免主题闪烁。

`web/dist` 为有意提交到仓库的前端构建产物（Linux 原生管理器等直接复用）；改动 `web/src/` 或 `web/` 构建配置后，由 pre-commit 钩子（`.githooks/pre-commit`，`core.hooksPath` 已配置）自动重建并暂存，手动构建仍可用 `cd web && npm run build`。

登录后前端会把“自选股 + 我的持仓”合并去重，在后台调用行情预取接口。报价自动刷新频率按账户保存，可选 5/10/15/30/60/120 秒，默认 5 秒；手动刷新会先定向刷新当前选中证券并并行刷新可见 K 线，其他证券再在后台补全。后复权日 K 预热和缓存周期为 5 分钟；1/5/15/30/60 分钟线按需分段加载，最新分段最长缓存 30 秒并在页面停留、重新聚焦时自动检查。收盘后最后一根分钟线正常停在当日 15:00；后端会拒绝把上一交易日的数据标记为实时。实时行情缓存始终与研究、评分、报告和回测的不可变快照隔离。
自选、我的持仓和账户总资金按登录用户保存到 PostgreSQL，可在“我的持仓”页新增、编辑或删除。账户总资金包括持仓市值和可用现金；当前仓位按最新行情（不可用时按成本价估算）乘以持仓数量后除以账户总资金自动计算。Cookie 只承载认证会话，清除 Cookie 后重新登录同一账户仍会恢复资产数据；部署前仅存在浏览器内存中的自定义数据无法追溯，新表上线后从默认值开始并在首次修改后永久保存。这些手工数据只用于页面记账与行情预取，不会覆盖研究生成的版本化组合，也不会触发真实下单。

持仓页可启用盘中盈利退出监控、止损预警和自选股买入区间监控。交易日 09:30–11:30、13:00–15:00 每分钟合并行情后检查；止损线优先使用手工价格，缺失时以 20 个后复权日 K 的 ATR 推导并限制在成本价下方 5%-10%，历史不足时使用 8%。触发止损时先写入高优先级通知，再排队快速 AI 退出研究；用户也可手动提交某个现有持仓的退出研究。正式评分合格且 Trade Plan 为 `BUY` 的自选股，仅在下一个交易日的有效入场区间首次命中时提示。所有路径只创建通知、研究和模拟方案，不自动买入、卖出或修改持仓。「卖出建议」页把上述自选股买入、卖出与止损提醒集中为交易建议中心：每只自选股可独立开启/暂停自动建议、查看 AI 买入/卖出目标与止损价、覆盖自定义价格并查看提醒状态；页面优先展示股票名称（行情不可用时退回代码）。

研究中心提供持久化 AI 问答。用户可询问任意问题，也可用 `@名称`、`@002138` 或 `@600690.SH` 附加由已提交证券主数据精确解析的行情、近 30 根日 K、个人持仓、最近正式评分和强相关新闻；同名歧义会拒绝绑定。启用联网时通过同一 Compose 私有网络中的 SearXNG 检索最多 5 条 Google、Bing 或 DuckDuckGo 结果，公共检索按 SHA-256 查询键缓存，含“最新、今日、实时”等时效词缓存 5 分钟，普通查询缓存 30 分钟，并以 Redis 单飞抑制并发重复请求。无历史 `decision_at` 时服务端在并行数据返回后冻结实时决策时点；显式历史时点只读取 PIT 合格数据并禁止联网，避免未来信息。模型不会获得数据库、凭据或任意内网访问能力。

### 原生管理器应用

不使用 Docker/WSL 时，可用 [`docs/NATIVE_WINDOWS.md`](docs/NATIVE_WINDOWS.md) 中的原生入口安装和管理运行组。

Windows 管理器位于 [`windows/native-control-center`](windows/native-control-center)，是测试版本，可能不稳定；它包含中文 WinForms GUI、命令行入口和单文件安装包：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\native\ashare-native.cmd install
.\scripts\native\ashare-native.cmd start
.\windows\native-control-center\build.ps1
.\windows\native-control-center\AshareAI.NativeControlCenter.exe
.\windows\native-control-center\AshareAI.NativeControlCenter.Cli.exe status --json
.\windows\native-control-center\ashareai.cmd logs --tail 200
```

`dist\AshareAI-Setup.exe` 是 Windows 单文件安装包，携带中文 GUI、CLI、`ashareai.cmd`、预构建 Web、固定 Python 安装器和固定 SearXNG 压缩包。安装后不需要 Docker Desktop、WSL、Node.js 或 Git。安装包支持无人值守部署：

```powershell
.\windows\native-control-center\dist\AshareAI-Setup.exe /quiet /dir "D:\AshareAI" /root "D:\AshareAI\runtime"
.\windows\native-control-center\dist\AshareAI-Setup.exe /quiet /start-services
.\windows\native-control-center\dist\AshareAI-Setup.exe /uninstall /quiet
```

直接双击 Windows 安装包时可以选择管理器目录和运行目录；默认管理器目录是安装包所在目录下的 `AshareAI`，默认运行目录是其下的 `runtime`。安装器把固定版本的 PostgreSQL、Redis 兼容服务、SearXNG 运行依赖、Python 环境和构建后的 Web 资源放在仓库外；`status -Json` 输出由 API、Web（嵌入 API）、Job Worker、Exit Advice Worker 及可选 Research Worker 组成的 Windows 进程组工作集。停止、诊断和再次启动均使用同一外部运行目录，源码树不接收运行数据、依赖二进制或本地凭据。

Linux 非 Docker 管理器位于 [`linux/native-control-center`](linux/native-control-center)，同样是测试版本，可能不稳定；它提供 Tkinter/ttk GUI 和同目录控制器，与 Windows 管理器保持相同的命令契约和状态 JSON 字段：

```bash
python3 linux/native-control-center/native_control_center.py
linux/native-control-center/ashare-native-linux.sh status --json
```

Linux 管理器覆盖安装更新、启动、停止、重启、修复、诊断、打开 Web、服务表、活动记录和看门狗日志。安装器会在源码树外创建私有 venv，安装锁定的 Python 依赖，复制 `web/dist`，探测 PostgreSQL/Redis-compatible/SearXNG 运行条件，并把绝对路径写入 `config/native-paths.json`。缺少系统级 PostgreSQL 或 Redis-compatible 二进制时，安装会明确列出缺失项而不会伪报成功；状态刷新仍可在未安装目录上快速返回。

### 完整 Docker 栈

> 服务器首次安装、HTTPS 反向代理、GHCR 镜像部署、独立 SearXNG、升级、备份和故障排查请见 [Docker 服务器部署教程](docs/DOCKER_DEPLOY.md)。

Compose 先读取未跟踪的 `.env` 以保留管理员、模型 API 和配置加密密钥，再读取独立的
`.env.docker` 覆盖数据库、Redis、对象存储等容器地址。外部 HTTPS 模型网关无需容器侧
覆盖；只有模型网关运行在宿主机时才使用 `host.docker.internal`。

```powershell
Copy-Item .env.local.example .env
Copy-Item .env.docker.example .env.docker
# 在 .env 中填写 ADMIN_PASSWORD、LLM_API_KEY，并生成 Fernet 格式的
# MODEL_SETTINGS_ENCRYPTION_KEYS（真实凭据均不得提交）
docker compose -p ashare-ai-src -f compose.yaml up -d --build
```

默认 Compose 面向小型主机，建议至少 2GB 内存：包含 Nginx WebGUI、API、单一串行 `job-worker`、独立低并发 `exit-advice-worker`、
PostgreSQL、Redis 和仅在 Compose 私有网络内可访问的 SearXNG，并为服务设置内存/PID/日志上限。`job-worker` 同时承担收盘后调度，按
Research、Trade Plan、Backtest 等 Redis 租约队列逐个取任务；退出研究由独立 Worker 消费，避免与普通重任务争抢队列。周期清理、退出研究和普通重任务均在隔离子进程中
执行，结束后释放 Pandas/PyArrow 等科学计算堆，避免多个 Worker 并发触发 OOM。WebGUI 由
Nginx 提供静态文件并把 `/api` 反向代理到 FastAPI；本地浏览器访问 `http://localhost`。
PostgreSQL、Redis 和 API 默认仅绑定 `127.0.0.1`，Redis 强制密码认证。

手动启动每日研究时，Web 会先要求选择标准模式或“至高模式”。至高模式由版本化
`configs/supreme_mode.v1.json` 约束，Worker 在真正采集前读取自身 cgroup 内存余量、CPU 配额和即时 CPU 负载，计算本次 AKShare 行情、财务、公告、新闻和分红采集的有界线程数；内存余量或 CPU 压力不足时会自动退回到更低并行，最低为单路。它不会提高 `llm_agent_max_concurrency`，因此不会向模型网关施加额外并发压力。选择、策略版本/哈希、实际执行档案、资源等级和原因码都会写进不可变运行 Manifest 与审计；自动定时报告固定使用标准模式。相同范围与预算的活动研究仍会复用已有任务，避免只因启动速度偏好重复采集和生成报告。

内置流水线使用 `object-data` 内容寻址卷。仓库不再捆绑安全更新滞后的 MinIO/MC 镜像；确需
S3 兼容时，通过 `OBJECT_STORE_ENDPOINT` 接入受维护的外部 S3 服务。管理员在“系统设置”中可查看
运行环境内存、CPU、磁盘及 API/Worker 占用；所有修改与恢复操作均须由当前登录管理员输入自己的账户密码解锁，
解锁证明仅保留 10 分钟且绑定当前会话。管理员可将研究拓扑保存为 `SERIAL`（仅 `job-worker` 消费研究）或 `DUAL`（固定两个 `research-worker`
消费研究，`job-worker` 跳过研究队列）。默认 `SERIAL` 不创建 `research-worker` 容器；保存为
`DUAL` 后，执行设置页面给出的 Compose 命令才会启用 `dual-research` profile 并启动两个副本。切回
`SERIAL` 时该命令会停止研究 Worker。`DUAL` 内存容量按实测基线和两个 Worker 的容器预算分级提醒，不再使用固定 4GB 阈值拒绝保存；模型网关并发容量仍是硬性门禁。不要手工
启用会竞争默认 Trade Plan、回测或研究队列的 legacy `parallel-workers` profile。

公网部署先复制 `.env.production.example` 为未跟踪的 `.env`，逐项填写强随机数据库、Redis、
管理员密码和加密密钥，再复制 `.env.docker.example`。将 `TRUSTED_HOSTS` 设置为实际域名，
将 `MODEL_ALLOWED_HOSTS` 设置为管理员可配置的模型网关主机白名单；生产模型 URL 只允许
HTTPS 和该白名单。Nginx 端口继续绑定 `127.0.0.1`，由同机 Caddy/Nginx 或受信任云负载
均衡器终止 TLS，禁止直接暴露 API、PostgreSQL、Redis 或对象存储端点。

启动后使用以下命令验收服务和配置：

```powershell
docker compose -p ashare-ai-src -f compose.yaml ps
docker compose -p ashare-ai-src -f compose.yaml exec api ashare-ai doctor
docker compose -p ashare-ai-src -f compose.yaml logs job-worker --tail 100
```

生产调度默认使用仓库内置的 `ApplicationPipeline + BuiltinDailyBackend`：开发环境会生成确定性全 A 风格 demo bundle，完整跑通股票池、三类特征、严格 Agent Schema、综合评分、预测分位、事件风控、15 股组合和日报；生产环境必须通过 `ASHARE_CANONICAL_BUNDLE` 提供强类型 canonical JSON，否则 fail closed。也可替换 `ASHARE_STAGE_BACKEND_FACTORY` 接入获授权的数据源与模型实现，而不修改编排图。
当前生产默认使用 `configs/first_release.v3.json`：保留 15/8%/25%/20%、风格限额、熔断、容量阈值和三类必需基准，并新增聊天缓存、止损、买入监控和通知保留策略。`configs/first_release.v1.json` 与 `configs/first_release.v2.json` 保留用于历史结果回放；每次运行都会把实际配置版本和文件哈希写入 Manifest。

### 开发检查

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy src
cd web
npm test -- --run
npm run build
```

### GitHub 自动构建

向任意分支推送提交后，GitHub Actions 会自动执行两类构建；推送到 `main` 分支或 `v*` 格式的版本标签时，
还会发布可直接从 GHCR 拉取的 Docker 镜像：

- 将应用镜像 `docker/app.Dockerfile` 发布为 `ghcr.io/<GitHub 用户名>/ashare-ai-research`；
- 将 Web 镜像发布为 `ghcr.io/<GitHub 用户名>/ashare-ai-research-web`；
- 将 PostgreSQL 定制镜像发布为 `ghcr.io/<GitHub 用户名>/ashare-ai-research-postgres`。三类镜像的
  `main` 分支都会更新 `latest`，发布构建还会生成分支、版本或 `sha-<commit>` 标签；
- 使用 PyInstaller 生成 Linux x86_64 和 Windows x86_64 的独立 `ashare-ai` CLI，并将两个文件作为
  GitHub Actions 构建产物保存 14 天。

也可以在仓库的 Actions 页面手动运行 `Build and publish`。独立可执行文件包含 CLI 所需的迁移、配置和
报告模板资源，但运行 API 或迁移仍需提供外部 PostgreSQL/Redis 等服务；不要把 `.env`、密码或 Token
打包进产物。

### 直接使用 GHCR 镜像

仓库提供 [compose.ghcr.yaml](compose.ghcr.yaml)，会移除本地 `build` 配置，改为直接
拉取 GHCR 的 Web、应用和 PostgreSQL 镜像。先将 GHCR 对应的三个 Package 设置为 **Public**（否则需要
先执行 `docker login ghcr.io`），然后：

```powershell
Copy-Item .env.production.example .env
Copy-Item .env.docker.example .env.docker
# 在 .env 中填写生产数据库、Redis、管理员密码和加密密钥
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml pull
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml up -d
```

默认镜像地址对应本仓库；如果是 Fork，运行前设置 `ASHARE_APP_IMAGE`、`ASHARE_WEB_IMAGE` 和
`ASHARE_POSTGRES_IMAGE` 为自己的 GHCR 地址即可。该方式仍然是完整多服务栈，不把 PostgreSQL、Redis
和 API 强行塞进一个容器；访问入口仍为 Nginx WebGUI 的 `http://localhost`。

## API

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `POST /api/v1/auth/token`
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/revoke`
- `GET /api/v1/auth/me`
- `GET /api/v1/app/bootstrap`
- `GET|PUT /api/v1/assets`
- `PUT /api/v1/assets/exit-monitor`
- `PUT /api/v1/assets/market-refresh`
- `GET /api/v1/exit-advice`
- `GET /api/v1/exit-advice/{advice_id}`
- `POST /api/v1/exit-advice/manual`
- `GET|PUT /api/v1/buy-entry-monitors`
- `GET /api/v1/notifications`
- `GET /api/v1/notifications/summary`
- `GET /api/v1/notifications/{notification_id}`
- `POST /api/v1/notifications/read`
- `POST /api/v1/notifications/read-all`
- `POST /api/v1/devices`
- `DELETE /api/v1/devices/{device_id}`
- `POST /api/v1/devices/{device_id}/deliveries`
- `GET /api/v1/securities/resolve`
- `GET /api/v1/ai/models`
- `GET /api/v1/ai/chat/metrics`
- `GET /api/v1/ai/costs?days=30`
- `GET|POST /api/v1/ai/chat/threads`
- `GET /api/v1/ai/chat/thread-index`
- `PATCH|DELETE /api/v1/ai/chat/threads/{thread_id}`
- `POST /api/v1/ai/chat/threads:bulk-delete`
- `GET /api/v1/ai/chat/threads/{thread_id}/messages`
- `POST /api/v1/ai/chat/threads/{thread_id}/messages:stream`
- `POST /api/v1/ai/chat/attachments`
- `GET /api/v1/ai/chat/attachments/{attachment_id}/content`
- `POST|GET|DELETE /api/v1/me/data-exports[/{export_id}]`
- `GET /api/v1/me/data-exports/{export_id}/download`
- `POST|GET /api/v1/me/data-imports[/{import_id}]`
- `POST /api/v1/me/data-imports/{import_id}/apply`
- `GET|POST /api/v1/admin/users`
- `PATCH /api/v1/admin/users/{user_id}`
- `GET|PUT|DELETE /api/v1/admin/system-settings`
- `DELETE /api/v1/admin/system-settings/{field}`
- `GET /api/v1/market/quotes?symbols=600000.SH,000001.SZ`
- `GET /api/v1/market/quotes/{symbol}`
- `GET /api/v1/market/klines/{symbol}?period=1m|5m|15m|30m|60m|day`
- `POST /api/v1/market/prefetch`
- `GET /api/v1/market/status`（兼容新增 `market_session`，用于显示 A 股开闭市状态）
- `GET /api/v1/search/financial?q=贵州茅台股价`
- `GET /api/v1/search/status`
- `POST /api/v1/research/runs`
- `GET /api/v1/research/runs/{run_id}`
- `POST /api/v1/research/runs/{run_id}/cancel`
- `GET /api/v1/research/runs?limit=5&trading_date=YYYY-MM-DD`
- `GET|PUT /api/v1/research/settings`
- `GET /api/v1/runs`
- `GET /api/v1/health`
- `GET /api/v1/reports/{trading_date}`
- `GET /api/v1/candidates/{trading_date}`
- `GET /api/v1/portfolios/{trading_date}`
- `GET /api/v1/scores/{trading_date}/{symbol}`
- `GET /api/v1/scores/{trading_date}/{symbol}/lineage`
- `POST /api/v1/backtests`
- `GET /api/v1/backtests/{backtest_id}`
- `POST /api/v1/backtests/{backtest_id}/retry`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/audit`
- `GET /api/v1/reports/{report_id}/content`
- `GET /api/v1/reports/{report_id}/symbols`
- `POST|GET /api/v1/reports/{report_id}/trade-plans`
- `GET /api/v1/trade-plans/{plan_id}`

除健康检查和登录外，业务接口均要求登录；修改类请求还必须携带登录时下发的 CSRF
Cookie 对应请求头。原生手机客户端通过 `/api/v1/auth/token` 获取 Bearer access/refresh token，
随后调用 `/api/v1/app/bootstrap`，一次取得当前用户、自选股与持仓、研究范围和限制、新功能
开关及相关资源路径；Bearer 修改请求不使用 Web CSRF Cookie。`WATCHLIST` 研究可在 `symbols`
中提交自选股/持仓的任意非空子集；省略 `symbols` 继续兼容旧客户端并研究全部已保存标的。
报告的 `/symbols` 资源会返回每只股票的正式研究状态、确定性评分、数据门禁原因和交易建议
资格，符合资格的股票可由报告 Trade Plan 接口生成购买建议。每日研究、回测和 Trade Plan 提交写入 PostgreSQL 后进入各自 Redis
pending/processing 确认队列；手机客户端可使用 `Idempotency-Key` 安全重试，服务端按用户、路由、Key 和请求体哈希返回首次创建的资源，同 Key 改变请求体会返回 `409`。
密码登录和 Token 签发共享按来源地址计算的失败尝试限流，Nginx 同时按不可伪造的直连来源
执行边缘限流；超过阈值返回 `429` 和
`Retry-After`；成功认证会清除该来源的失败计数。业务 API 默认返回 `Cache-Control: no-store`
及 CSP、HSTS 等浏览器安全响应头，生产环境关闭 Swagger、ReDoc 和 OpenAPI 公共入口。本地 Compose 的
Web、API、PostgreSQL 和 Redis 默认只绑定 `127.0.0.1`；物理手机联调或正式部署不得
直接暴露数据库、Redis 或对象存储，API 必须通过受信任的 HTTPS 反向代理发布并启用安全 Cookie。
实体手机在受信任局域网内联调时，只把 `WEB_BIND_ADDRESS` 显式设为主机局域网地址或
`0.0.0.0`；保持 `SERVICE_BIND_ADDRESS=127.0.0.1`，手机统一经 Nginx 的 `/api` 访问后端。
每个运行都记录用户归属、状态、失败原因和审计事件。
每日研究可由任务所有者请求安全停止：排队任务直接进入 `CANCELLED`，运行中任务先进入
`CANCEL_REQUESTED`，并在当前原子阶段完成后停止；终态任务不会被重新取消。
`FUSED` 是已完成的观察模式终态：评分、候选和对应 `run_id` 的报告可正常读取，但不会
生成模拟组合；组合接口会返回明确的观察模式状态，不会回退到同日旧组合。每日研究页按
交易日展示最近 5 次运行及阶段、进度、失败原因和报告入口。

行情以 AKShare 为主。API 启动时会预热一个可复用的隔离行情进程，NumPy、Pandas 和
PyArrow 不会进入 API 父进程，也不会在每次请求时重复导入。实时快照使用共享 5 秒缓存与
单次上游请求合并；日 K 使用独立的 300 秒缓存。日线 AKShare 超过配置的短延迟阈值时会
并发请求腾讯 HFQ 日线并采用首个有效结果，分钟线仍保持 AKShare HFQ 主路径。
`POST /api/v1/market/prefetch` 单次最多接受 50 个去重后的标准代码，默认并发
数为 4；默认并行执行一批报价和逐股票后复权日 K 请求。传入 `include_quotes=false` 时只预热
日 K，供首页在报价请求完成后的空闲时段使用，避免重复读取报价。单个股票失败只写入 `errors`，不会
丢弃其他成功结果。该接口要求登录和 CSRF 请求头。AKShare 异常时，
无需密钥的新浪公开接口会提供实时报价和分钟线，腾讯公开接口提供后复权日线；配置
`TUSHARE_TOKEN` 后会作为额外备用。响应会标注实际来源，且只在所有上游均不可用时才返回
最近成功缓存并标记延迟。分钟线和日线统一按后复权（`hfq`）契约返回。实时行情服务不写入
研究或回测快照；研究和回测仍只读取带 `symbol + trading_date + available_at` 的已提交不可变数据。

行情相关配置：

```env
MARKET_CACHE_SECONDS=5
MARKET_KLINE_CACHE_SECONDS=300
MARKET_PREFETCH_MAX_WORKERS=4
MARKET_PROVIDER_MAX_WORKERS=4
MARKET_PROVIDER_MAX_QUEUE=8
MARKET_CACHE_MAX_ENTRIES=512
MARKET_STALE_SECONDS=900
MARKET_TIMEOUT_SECONDS=10
MARKET_HEDGE_DELAY_SECONDS=0.5
```

交互式金融搜索采用“AI 解析意图 + 确定性数据源取数”：标准证券代码和内置名称走直接
快速路径，其余自然语言由搜索模型生成严格校验的 `FinancialQueryIntent`，随后由 AKShare、
东方财富、新浪或腾讯适配器获取事实。首版支持单只股票或指数的行情、估值、K 线和最近
一期已披露核心财务指标，不回答开放式投资问题，也不支持板块或多实体比较。兼容配置：

```env
FINANCIAL_SEARCH_PROVIDER=neodata-financial-search
NEODATA_FINANCIAL_SEARCH_MODE=auto
NEODATA_FINANCIAL_SEARCH_PATH=C:/tools/neodata-financial-search/query.py
NEODATA_FINANCIAL_SEARCH_TIMEOUT_SECONDS=15
FINANCIAL_SEARCH_CACHE_SECONDS=15
FINANCIAL_SEARCH_MAX_CONCURRENCY=4
FINANCIAL_SEARCH_RATE_LIMIT_PER_MINUTE=30
```

`neodata-financial-search` 仍作为代码/新浪兼容回退层，`neodate-financial-search` 误拼也会
映射到正式名称。搜索结果属于实时、非 PIT 的交互数据，不会进入冻结研究、确定性评分或
回测快照。响应明确给出意图结果、实际上游、抓取时间、报告期/公告日、来源和警告；财务
记录会拒绝查询时点之后披露的数据。NeoData CLI 子进程仍使用最小环境且不会继承 LLM 密钥。
API 还对相同查询执行 15 秒缓存和 single-flight 合并，默认最多并行 4 个上游查询，
每个登录用户每分钟最多 30 次，以免搜索子进程挤占业务线程。

每日研究默认 `CANONICAL_BUNDLE_MODE=akshare`：HTTP 提交只冻结交易日、决策时点、
供应商和版本配置，真实证券、未复权历史与基准数据由 Research Worker 异步采集后写入
不可变对象和 Manifest。免费链路读取东方财富个股新闻、财新免费新闻流、巨潮官方公告与
现金分红，并用新浪免费分红记录交叉补充；同时读取新浪三大财报，以报告期、公告/更新
时间执行 PIT 过滤；单股票数据失败或字段不完整时才写入明确标注的中性占位证据，不会
伪装成真实基本面或公告结论。数据门禁按股票执行：不完整股票仍保留评分并进入正式报告，
但固定显示 `NO_BUY`，不进入正式组合候选。`WATCHLIST`/`CUSTOM` 正式可用股票少于版本化
组合目标时，研究仍以 `SUCCEEDED` 完成，只记录组合未生成；全市场研究保留严格组合门禁。
`FUSED` 仅用于最大回撤等全局风险熔断。
WebGUI 可选择动态市场股票池、从当前用户的自选与持仓中自由勾选任意非空子集，或手工输入
单只/多只 A 股代码；自选范围提交的股票会由 API 再次校验归属并冻结到 Manifest，未勾选股票
不会进入本次研究。
定向研究只对通过股票池可交易性校验的指定证券生成特征、评分、候选和报告。研究请求还可
冻结总资金预算、单股最高投入和最高可接受股价：全市场或达到版本化组合目标的定向研究会
把这些限制用于下一交易日模拟组合试算。小规模定向研究仍发布覆盖全部目标股票的正式逐股
报告，并为合格股票冻结用途为 `SINGLE_SYMBOL_ADVICE` 的验证快照，但不把单股信号伪装成
组合信号，也不放宽组合约束。研究范围和预算均进入运行输入哈希，表单中的实时价格只用于
下单手数预览，不进入不可变研究快照。
每位用户的自动日研默认关闭；研究页的设置悬浮窗提供报告 A、B 两套独立配置，可分别启停并设置
全市场、动态自选与持仓或手工股票范围，以及总预算、单股最高投入和最高可接受股价。开启一套即
运行单报告，两套均开启则在同一交易日提交两份独立任务，由默认串行 Worker 依次执行；配置相同
也按 A/B 槽位生成两份报告。调度器在上海交易日 15:05 起检查数据就绪状态，未就绪时按配置间隔
重试至权威交易日历确定的下一交易日 09:25（上海时间）；数据未就绪时任务会以
`DATA_READINESS_WAITING` 持久化其冻结的 `decision_at`、范围和预算，待基准齐备后才构建快照，
并且绝不跨入下一交易时段重建前一日快照。自选与持仓在实际运行时动态读取，
为空只跳过对应槽位且不阻塞另一槽位。每次自动任务会把槽位、配置版本、范围和预算冻结进 Manifest
及输入哈希；同一用户、交易日和槽位只提交一次，当日首次提交后修改的配置从下一交易日起生效。
旧版 `auto_enabled` 设置请求仍兼容。手动任务使用独立 `MANUAL` 幂等键，
可以和同日 `AUTO` 任务并行。冻结快照模式由系统强制开启，界面只读展示。交易日开盘后请求
昨日研究时，只允许复用当前用户已有的该日不可变 bundle（包括原任务已完成数据采集、但在
Agent 或模型网关阶段失败的运行）；复用前校验运行归属、交易日和 SHA-256，绝不使用盘中实时
行情重构历史数据。没有可复用 bundle 时返回明确的 `409`，需等收盘后重新采集。
配置 `TUSHARE_TOKEN` 后，Research Worker 也只在免费源失败、历史缺失或财报/公告字段
不完整时补用 Tushare，并把各数据集的实际来源写入冻结记录。
Demo 数据必须同时显式设置 `CANONICAL_BUNDLE_MODE=demo` 和 `ALLOW_DEMO_DATA=true`。
管理员可在 WebGUI 的“模型设置”页配置 OpenAI-compatible Responses API，并为每个已配置模型维护 `GROK`、`OPENAI` 或 `COMPATIBLE` 缓存档案、上下文窗口、输出/推理预留和每百万 token 单价。旧配置默认保持 `COMPATIBLE`，不会向未知网关发送专属缓存字段。聊天会按安全预算保留完整最近轮次；固定 PIT 上下文或当前问题超预算时明确拒绝。Grok 对话内部按严格追加消息链重放私有动态快照，OpenAI 可使用稳定缓存键和增量 Responses 续接；快照不会出现在消息、导出或公开 API 中。聊天页会显示本轮与近 30 天输入、缓存读取/写入、未缓存输入、命中率以及按管理员单价估算的支出和节省。配置以不可变
版本保存，API Key 使用 `MODEL_SETTINGS_ENCRYPTION_KEYS` 加密且永不回传；搜索模型与研究
模型可分别设置。启用新版本前必须通过严格 JSON Schema 连通性探测，失败时旧版本继续
生效。每次日研会把配置 ID、版本和哈希固定到 Manifest，排队或运行中的任务不会跟随
后续热切换。未启用模型配置时仍可使用确定性的内置 Agent 完成合规验收。

AI 股票问答支持用股票名称 `@`提及、置顶/分组/归档/搜索/批量删除、幂等 SSE 重放和 PNG/JPEG/WebP/非动画 GIF。图片按用户隔离加密，自上传成功起固定保留 7 天，到期瞬间停止读取并由 Worker 物理清理。当前用户可在“个人档案”页导出加密完整档案，或上传后先预览、再分类合并；任何图片和账户凭据均不进入档案。格式详见 [`docs/PERSONAL_ARCHIVE.md`](docs/PERSONAL_ARCHIVE.md)。
AKShare 每个证券列表、股票历史或基准历史逻辑请求默认执行两轮受限尝试，每轮依次访问东方财富和新浪；空响应、连接中断、超时和 JSON 解码失败均会触发备用源或下一轮。可通过 `AKSHARE_FETCH_MAX_ATTEMPTS=2` 和 `AKSHARE_FETCH_BACKOFF_SECONDS=1` 调整轮数与轮间退避。单个非必需股票失败会脱敏记录并跳过，但有效标的仍不得少于 15；证券列表或基准在全部尝试后失败时任务安全终止，不会使用前一日缓存或不完整数据冒充当日快照。
新闻风险按来源可信度分层：巨潮等官方来源可产生 HIGH/CRITICAL，免费媒体重大负面最多为
MEDIUM，不能单独硬阻断；跨源新闻按标题、日期和内容哈希去重。评分只使用
`available_at <= decision_at` 的近 30 日新闻以及决策前已实施现金分红。

完成的日研会生成带用户归属、文件哈希、日历范围和 PIT 信号的 `backtest_bundle`
快照，回测工作台可直接选择。首次单日研究尚没有后续执行日，因此不会暴露为可执行
回测快照；累计至少两个不同研究日后即可形成可执行快照。Worker 使用 Redis 租约和
心跳，只回收租约过期任务，避免扩容或重启时重复抢占其他 Worker 的运行。
`FUSED` 运行仍会生成明确标注“观察模式”的研究模拟快照，但不会生成可执行组合或
实盘建议；Manifest、报告和 API 会区分“完整股票不足”和“最大回撤熔断”，并记录正式
可用股票、被排除股票及原因码。累计快照承接上一份已提交 Parquet，避免用当前股票集合
重写历史持仓。

`builtin_backtest.write_backtest_bundle` 生成标准 `kind + payload_json` Parquet bundle；
Backtest Worker 校验 Manifest 状态、文件哈希、RAW 价格和时点约束，再写回绩效、三基准、
容量、归因、产物哈希和审计事件。
证券行业分类允许随研究日变化。行业总损益按证券终止暴露日（期末仍持有则为回测结束日，
否则为最后成交日）之前最近一次研究信号的行业归集；分类变化会写入结果 `warnings` 和
归因产物。失败回测不会自动重跑，所有者可在工作台确认后手动重试；重试会重新校验已提交
快照及文件哈希，保留原失败审计并清空旧结果引用后重新排队。

日报 HTML 仍是不可变沙箱内容；报告页外层可以选择同一 `run_id` 的正式候选股，异步生成
模拟交易方案。确定性优化器最多读取 240 个已冻结交易日，隔离训练/样本外窗口，枚举限价、
止盈、止损、移动止盈和最长持有期，并复用涨跌停、T+1、费用、滑点和容量规则。样本外净
收益不为正、最大回撤超过 12% 或完整交易少于 5 笔时输出 `NO_BUY`，不会放宽条件。AI 仅解释
确定性方案，模型不可用时标记“AI解释未生成”，不影响数值结果。所有方案只用于研究、回测
和模拟盘，不承诺未来收益、不自动下单。

## 供应商与授权

最小安装不包含 AKShare、Tushare Pro 和 Qlib 的重型依赖。按需安装 `providers`、`quant` 或 `orchestration` extra。生产使用必须遵守数据授权、平台条款和访问频率限制；东方财富等聚合来源的公告必须优先用交易所正式披露源核验。

## 免责声明

系统输出仅用于研究、回测和模拟盘，不构成投资建议，也不执行实盘订单。

## 开源许可证

本项目采用 [Apache License 2.0](LICENSE) 开源。使用、修改或分发本项目时，请遵守许可证条款，
并保留所要求的版权及许可证声明。第三方数据、模型、服务和依赖仍适用各自的许可与使用条款。

当前重大版本的安全修正版为 v2.0.3，完整变化与升级步骤见
[中文发布说明](docs/releases/v2.0.3.zh-CN.md)。v2.0.0 的重大版本说明保留在
[初始发布记录](docs/releases/v2.0.0.zh-CN.md)。历史首个稳定版见
[v1.0.0 中文发布说明](docs/releases/v1.0.0.zh-CN.md)和
[English Release Notes](docs/releases/v1.0.0.en.md)。
