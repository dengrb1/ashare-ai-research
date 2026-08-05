# A 股 AI 自动投研系统：仓库协作说明

## 范围与分层

- 本仓库用于收盘后 A 股投研、评分、模拟组合和事件驱动回测；首版不接自动实盘下单。先读相关代码、测试、配置、迁移和 `README.md`，以实现和版本化工件为准，不凭摘要臆造接口或规则。
- 技术栈：Python 3.11+、FastAPI、SQLAlchemy/Alembic、PostgreSQL、Redis、Parquet/DuckDB、MinIO/S3；前端在 `web/`，为 React + Vite + TypeScript。
- `core/` 管契约、配置、哈希和 PIT；供应商接入在 `adapters/`、`ingestion/`、`market/`，不得泄漏供应商字段。特征、Agent、评分和预测在 `features/`、`agents/`、`scoring/`、`quant/`；Agent 只输出 Pydantic 校验的子分和证据，Manager 只归纳。
- 交易、组合、回测在 `universe/`、`trading/`、`portfolio/`、`backtest/`；存储、审计、报告、调度、API、检索在 `storage/`、`reports/`、`observability/`、`orchestration/`、`api/`、`search/`。异步改动同时检查 API、Worker、运行和审计记录。
- 前端 API 集中于 `web/src/api.ts`，共享类型在 `web/src/types.ts`；领域单测放 `tests/unit/`，外部链路放 `tests/integration/`。

## 不可突破的规则

- 研究、特征、评分、组合和回测均须按 `symbol + trading_date + available_at` 可追溯；决策必须显式给出 `decision_at`，拒绝 `available_at > decision_at`。
- 只读取已提交的不可变 Manifest/快照。原始载荷、Parquet、模型、报告记录来源、抓取时间、版本和 SHA-256；DuckDB 不得绕过 Manifest。
- 最终评分只能来自可复现、版本化的确定性公式。生产记录配置哈希；权重、限额、熔断、容量和基准以 `configs/first_release.v1.json` 为准。
- 涨跌停、T+1、停复牌、申报单位、费用、新股和市场差异必须来自带生效日期的规则数据。规则缺失、冲突或不匹配时拒绝交易或仅发布观察报告，禁止硬编码猜测。
- 组合和回测必须通过领域模型闭环验证目标持仓、行业/换手约束、事件风险、费用、基准、复现哈希和最大回撤熔断；不可只在 UI 展示。

## 变更契约

- API、Worker、前端调用领域服务；不要在路由、页面或适配器复制评分、风控、交易规则。公开 API、Pydantic 模型、任务载荷、表或规则配置变更时，同步调用方、迁移、类型、测试和必要文档，并保持兼容或给出迁移路径。
- 数据库结构只用新 Alembic migration；迁移可重跑，说明锁表、回滚和大表风险。新配置必须版本化、有校验和默认拒绝策略；不改变已有版本化配置的含义或散落业务常量。
- 前端保持认证、HttpOnly 会话、CSRF 双提交和主题初始化；Token、密码和内部错误不得进入 `localStorage`、日志或页面。代码需可维护、无已知报错，并配套相称测试、静态检查和失败处理。

### Edge Gateway

- Docker、Windows 原生和 Linux 原生提供相同的 FRP TOML、结构化 Nginx 主机、校验、版本、回滚、启停和应用状态，且共用 Pydantic/API 契约、资源上限和安全策略；不得以平台特例绕过 API、认证、审计或代理目标限制。
- 管理界面和控制器必须在下一次读取/轮询时发现外部文件修改并导入新的不可变版本：Docker 读取挂载的 `docker/edge-gateway`，原生读取 `<runtime>/config/edge-gateway`。敏感 `frpc.toml` 与生成的 `managed.conf` 不提交。
- 保存经受限控制器或原生进程原子写回，执行语法校验和 SHA-256 检测。目录不可写时保留数据库版本并显示待应用；冲突时保留旧版本并拒绝覆盖。日志不得泄漏 FRP token 或完整敏感文件。

## `/api/v1` 与手机客户端

- `/api/v1` 是 Web 和 App 共用契约；新增 App 能力复用资源接口和 Pydantic 模型，不建直连数据库的专用后门。字段仅新增且有安全默认；破坏性变更新建 `/api/v2`，保留旧版并提供迁移/弃用期。
- Web 使用 Cookie 会话；App 仅用 Bearer：`POST /api/v1/auth/token`、`/auth/refresh`、`/auth/revoke`。不得让 App 模拟 Cookie/CSRF，也不得关闭 Web CSRF。令牌仅进系统安全存储，禁止写入普通偏好、日志、崩溃上报、剪贴板、示例、夹具或错误响应。
- 响应使用稳定 JSON：带时区 ISO 8601 时间、`YYYY-MM-DD` 日期、明确金额/价格/比例单位与精度、标准股票代码；不泄漏 ORM、供应商字段、异常栈、路径、桶地址或未脱敏审计信息。列表和时序定义稳定排序、`limit` 上限和 cursor；大对象按需获取。
- 有副作用/异步任务必须用“用户 + 路由 + `Idempotency-Key` + 请求体哈希”去重；耗时请求返回 `202` 和可轮询资源，取消幂等且保留审计。缓存、ETag、摘要和增量刷新不得绕过权限、PIT、Manifest 或报告版本。
- 所有 App 路由执行 `require_auth`、角色/归属、限流和统一错误；客户端的用户、权限、状态、评分、交易参数、`decision_at` 与哈希均不可信。交易建议始终仅适用于模拟组合。
- 改认证、公开 API、异步载荷或响应时，同步 OpenAPI、`README.md`、Web、客户端契约和测试，至少覆盖 Bearer、续期/撤销、401/403/422/429、分页、空结果、幂等重试、任务状态和旧客户端。`docs/API.md` 必须与实际 FastAPI 路由、字段、状态码、排序和权限一致。

## 协作、验证与安全

- 开始代码任务先识别依赖和唯一文件 owner。主编排负责拆分、整合、验收；仅在明确授权且确有独立任务时使用一个直接子代理，子代理不得继续派发；默认最多七个并行任务。简单单文件、配置、格式、状态和已有测试不派发。每项任务一次主执行、至多一次定向修复，失败日志交回原执行者，不派多个 Agent 竞速。架构/规则/风控/审计/安全用 `gpt-5.6-sol` `xhigh`，跨模块核心和测试用 `high`，常规模块用 `medium`，扫描用 `gpt-5.6-terra` `xhigh`，机械整理用 `gpt-5.6-luna` `xhigh`；禁止 `max`。
- 每次代码更新交付前必须执行 `docker compose -p ashare-ai-src -f compose.yaml up -d --build`，随后运行 `docker compose -p ashare-ai-src -f compose.yaml ps` 并确认 Web、API、PostgreSQL、Redis、Worker 健康；受限时明确未验证项。始终使用显式项目名。
- 默认低内存链路先启动 `postgres redis`（不捆绑 MinIO，使用 `object-data` 卷或外部 HTTPS S3），再运行 `ashare-ai doctor`、`ashare-ai migrate`，最后启动 API 和串行 `job-worker`；不在容器内重复启动 API/Worker。Docker 故障先查同一项目的 `ps -a`、`compose ls --all`、`docker ps -a`；解析不到 `postgres`/`redis` 时先恢复并等待其 healthcheck，勿靠反复重启应用掩盖依赖故障。`Exited (0)` 且含 `SIGTERM` 是正常停止；不得删除数据库、Redis、lake 或 object 卷。
- 前端开发：`cd web; npm ci; npm run dev`，访问 `http://localhost:5173`，`/api` 代理 `http://127.0.0.1:8000`；完整栈经 `http://localhost`。Python 至少跑受影响的 pytest，跨层/数据/交易改动跑完整 `\.venv\Scripts\python -m pytest`（环境允许时）；前端至少跑 `cd web; npm test -- --run`。提交前跑相应 Ruff 与 mypy。
- `web/dist` 有意入库；修改 `web/src/` 或构建配置时，提交钩子会重建并暂存它。若跳过钩子，须手动 `cd web && npm run build` 并提交 `web/dist`。
- 不提交真实密码、Token、API Key、cookie、个人或生产数据；真实凭据仅在未跟踪 `.env`。提交前检查差异无凭据和无关生成物（`web/dist` 除外）。不破坏现有工作区或数据，不使用 `git reset --hard` 或 `git checkout --`；删除数据、生产迁移、触发任务或外部供应商调用前先获授权。
