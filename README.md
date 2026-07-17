# A 股 AI 自动投研系统（首版）

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

PostgreSQL 保存控制面、规则、运行、评分、组合和审计；Parquet 保存不可变数据快照与分析明细；DuckDB 只查询显式提交的 Manifest；MinIO/S3 保存公告原文、模型和报告。

## 本地开发

需要 Python 3.11、Node.js 20+。本地进程读取 `.env`；首次配置可从宿主机模板复制，随后只在未跟踪的 `.env` 中填写管理员和 LLM 凭据：

```powershell
Copy-Item .env.local.example .env
python -m venv .venv
.\.venv\Scripts\python -m pip install ".[dev]"
```

不要把真实密码、Token 或 API Key 写回任何 `*.example` 文件。仓库中的本地模板使用宿主机地址：PostgreSQL `127.0.0.1:5432`、Redis `127.0.0.1:6379`、MinIO `127.0.0.1:9000`；Docker 模板改用 `postgres`、`redis`、`minio` 服务名和 `/data` 容器路径。

### 本地完整链路

按以下顺序启动。若依赖服务由 Docker 提供，只启动基础依赖，不在容器里启动 API/Worker：

```powershell
Copy-Item .env.docker.example .env.docker
docker compose up -d postgres redis minio minio-init
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
$env:ADMIN_PASSWORD = "请替换为至少10位的强密码"
```

管理员通过 WebGUI 创建、禁用和重置其他账号。认证使用服务端数据库会话、
HttpOnly Cookie、双提交 CSRF 校验和 Argon2 密码哈希；禁用账号或重置密码会立即
撤销该用户现有会话。生产环境应设置 `COOKIE_SECURE=true` 并通过 HTTPS 暴露站点。

前端位于 `web/`，开发环境由 Vite 把 `/api` 代理到本地 FastAPI：

```powershell
cd web
npm ci
npm run dev
```

访问 `http://localhost:5173` 登录。全站默认浅色主题，登录页和主界面均可切换深色主题；选择保存在 `localStorage`，页面首屏脚本会在 React 启动前同步 `data-theme` 和浏览器 `theme-color`，避免主题闪烁。

登录后前端会把“自选股 + 模拟持仓”合并去重，在后台调用行情预取接口。报价仍按 15 秒刷新，后复权日 K 预热和缓存周期为 5 分钟；1/5/15/30/60 分钟线只在首次选择时按需加载并使用短期客户端缓存。实时行情缓存始终与研究、评分、报告和回测的不可变快照隔离。

### 完整 Docker 栈

Compose 先读取未跟踪的 `.env` 以保留管理员和 LLM 凭据，再读取独立的 `.env.docker` 覆盖容器地址。Docker 内调用宿主机 LLM 网关时固定使用 `host.docker.internal`，避免把容器内 `127.0.0.1` 误认为宿主机。

```powershell
Copy-Item .env.local.example .env
Copy-Item .env.docker.example .env.docker
# 在 .env 中填写 ADMIN_PASSWORD、LLM_API_KEY 等真实凭据
docker compose up --build
```

Compose 包含 Nginx WebGUI、API、Prefect 调度进程、每日研究 Worker、回测 Worker、
PostgreSQL、Redis 和 MinIO，并为基础服务配置健康检查。WebGUI 由 Nginx 提供静态文件并把
`/api` 反向代理到 FastAPI；浏览器只需访问 `http://localhost`。PostgreSQL、Redis、MinIO API/Console 同时开放 `5432`、`6379`、`9000/9001`，可供宿主机本地开发进程复用。

启动后使用以下命令验收服务和配置：

```powershell
docker compose ps
docker compose exec api ashare-ai doctor
docker compose logs research-worker backtest-worker --tail 100
```

生产调度默认使用仓库内置的 `ApplicationPipeline + BuiltinDailyBackend`：开发环境会生成确定性全 A 风格 demo bundle，完整跑通股票池、三类特征、严格 Agent Schema、综合评分、预测分位、事件风控、15 股组合和日报；生产环境必须通过 `ASHARE_CANONICAL_BUNDLE` 提供强类型 canonical JSON，否则 fail closed。也可替换 `ASHARE_STAGE_BACKEND_FACTORY` 接入获授权的数据源与模型实现，而不修改编排图。
首版的 15/8%/25%/20%、评分权重、风格限额、熔断、容量阈值和三类必需基准集中在 `configs/first_release.v1.json`；生产运行会把该文件哈希写入 Manifest，缺失时拒绝启动日跑。

### 开发检查

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy src
cd web
npm test -- --run
npm run build
```

## API

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/me`
- `GET|POST /api/v1/admin/users`
- `PATCH /api/v1/admin/users/{user_id}`
- `GET /api/v1/market/quotes?symbols=600000.SH,000001.SZ`
- `GET /api/v1/market/klines/{symbol}?period=1m|5m|15m|30m|60m|day`
- `POST /api/v1/market/prefetch`
- `GET /api/v1/market/status`
- `GET /api/v1/search/financial?q=贵州茅台股价`
- `GET /api/v1/search/status`
- `POST /api/v1/research/runs`
- `GET /api/v1/runs`
- `GET /api/v1/health`
- `GET /api/v1/reports/{trading_date}`
- `GET /api/v1/candidates/{trading_date}`
- `GET /api/v1/portfolios/{trading_date}`
- `GET /api/v1/scores/{trading_date}/{symbol}`
- `GET /api/v1/scores/{trading_date}/{symbol}/lineage`
- `POST /api/v1/backtests`
- `GET /api/v1/backtests/{backtest_id}`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/audit`
- `GET /api/v1/reports/{report_id}/content`

除健康检查和登录外，业务接口均要求登录；修改类请求还必须携带登录时下发的 CSRF
Cookie 对应请求头。每日研究和回测提交写入 PostgreSQL 后进入各自 Redis
pending/processing 确认队列；同一用户重复提交同一交易日的进行中研究会返回既有任务。
每个运行都记录用户归属、状态、失败原因和审计事件。

行情以 AKShare 为主。实时快照使用共享 15 秒缓存与单次上游请求合并；日 K 使用独立的
300 秒缓存。`POST /api/v1/market/prefetch` 单次最多接受 50 个去重后的标准代码，默认并发
数为 4；它并行执行一批报价和逐股票后复权日 K 请求，单个股票失败只写入 `errors`，不会
丢弃其他成功结果。该接口要求登录和 CSRF 请求头。AKShare 异常时，
无需密钥的新浪公开接口会提供实时报价和分钟线，腾讯公开接口提供后复权日线；配置
`TUSHARE_TOKEN` 后会作为额外备用。响应会标注实际来源，且只在所有上游均不可用时才返回
最近成功缓存并标记延迟。分钟线和日线统一按后复权（`hfq`）契约返回。实时行情服务不写入
研究或回测快照；研究和回测仍只读取带 `symbol + trading_date + available_at` 的已提交不可变数据。

行情相关配置：

```env
MARKET_CACHE_SECONDS=15
MARKET_KLINE_CACHE_SECONDS=300
MARKET_PREFETCH_MAX_WORKERS=4
MARKET_STALE_SECONDS=900
MARKET_TIMEOUT_SECONDS=10
```

交互式金融搜索默认使用 `neodata-financial-search`。系统会优先发现并调用其 `query.py`；
若本机或容器没有安装该 CLI，则自动使用相同新浪财经响应契约的内置兼容模式。配置示例：

```env
FINANCIAL_SEARCH_PROVIDER=neodata-financial-search
NEODATA_FINANCIAL_SEARCH_MODE=auto
NEODATA_FINANCIAL_SEARCH_PATH=C:/tools/neodata-financial-search/query.py
NEODATA_FINANCIAL_SEARCH_TIMEOUT_SECONDS=15
FINANCIAL_SEARCH_CACHE_SECONDS=15
FINANCIAL_SEARCH_MAX_CONCURRENCY=4
FINANCIAL_SEARCH_RATE_LIMIT_PER_MINUTE=30
```

`neodate-financial-search` 作为常见误拼也会映射到正式 Provider 名称。搜索结果属于实时、
非 PIT 的交互数据，不会进入冻结研究、确定性评分或回测快照。响应会分别标注 Provider
和实际上游 `sina-finance`，避免把兼容层误写成数据来源。生产镜像固定下载并校验
NeoData 提交 `369fd3961d3a1482005e9673a5fc635a7595e710`；CLI 子进程仅继承网络、证书和
临时目录所需的最小环境，不会继承数据库、管理员、对象存储、Tushare 或 LLM 密钥。
API 还对相同查询执行 15 秒缓存和 single-flight 合并，默认最多并行 4 个上游查询，
每个登录用户每分钟最多 30 次，以免搜索子进程挤占业务线程。

每日研究默认 `CANONICAL_BUNDLE_MODE=akshare`：HTTP 提交只冻结交易日、决策时点、
供应商和版本配置，真实证券、后复权历史与基准数据由 Research Worker 异步采集后写入
不可变对象和 Manifest。AKShare 无法提供可靠 PIT 基本面、公告或新闻时，系统写入明确
标注的中性占位证据并自动进入 `OBSERVE_ONLY/FUSED`，不会伪装成真实基本面结论。
配置 `TUSHARE_TOKEN` 后，Research Worker 也只在 AKShare 失败或历史缺失时补用 Tushare，
并把每条证券与 K 线的实际来源写入冻结记录。
Demo 数据必须同时显式设置 `CANONICAL_BUNDLE_MODE=demo` 和 `ALLOW_DEMO_DATA=true`。
Docker 模板默认使用确定性的 `AGENT_BACKEND=builtin`，因此宿主机 LLM 网关未启动时日研
仍可完成合规验收；需要启用模型 Agent 时改为 `openai_compatible`。此时容器侧
`LLM_BASE_URL` 必须使用 `host.docker.internal`，不能使用指向容器自身的 `127.0.0.1`。
AKShare 东方财富端点不可用时会自动降级到同一 SDK 的新浪证券、后复权日线和指数日线；
历史采集只允许在下一工作日 09:00 前冻结最近一个已完成交易日，更早日期必须提供已冻结
canonical 文件，防止用当前截面回构历史证券池。

完成的日研会生成带用户归属、文件哈希、日历范围和 PIT 信号的 `backtest_bundle`
快照，回测工作台可直接选择。首次单日研究尚没有后续执行日，因此不会暴露为可执行
回测快照；累计至少两个不同研究日后即可形成可执行快照。Worker 使用 Redis 租约和
心跳，只回收租约过期任务，避免扩容或重启时重复抢占其他 Worker 的运行。
`FUSED` 运行仍会生成明确标注“观察模式”的研究模拟快照，但不会生成可执行组合或
实盘建议；累计快照承接上一份已提交 Parquet，避免用当前股票集合重写历史持仓。

`builtin_backtest.write_backtest_bundle` 生成标准 `kind + payload_json` Parquet bundle；
Backtest Worker 校验 Manifest 状态、文件哈希、RAW 价格和时点约束，再写回绩效、三基准、
容量、归因、产物哈希和审计事件。

## 供应商与授权

最小安装不包含 AKShare、Tushare Pro 和 Qlib 的重型依赖。按需安装 `providers`、`quant` 或 `orchestration` extra。生产使用必须遵守数据授权、平台条款和访问频率限制；东方财富等聚合来源的公告必须优先用交易所正式披露源核验。

## 免责声明

系统输出仅用于研究、回测和模拟盘，不构成投资建议，也不执行实盘订单。
