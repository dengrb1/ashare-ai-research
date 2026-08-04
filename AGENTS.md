# A 股 AI 自动投研系统：仓库协作说明

## 项目边界与事实来源

- 本仓库是**收盘后**运行的 A 股投研、评分、模拟组合和事件驱动回测系统；首版不接入自动实盘下单。
- 先阅读与改动有关的代码、测试、配置和 `README.md`，以现有实现、数据库迁移、版本化配置和测试为准；不要凭 README 摘要臆造接口或业务规则。
- 后端为 Python 3.11+、FastAPI、SQLAlchemy/Alembic、PostgreSQL、Redis、Parquet/DuckDB、MinIO/S3；前端位于 `web/`，使用 React + Vite + TypeScript。

## 目录责任边界

- `src/ashare_ai/core/`：契约、配置、哈希、PIT 时点校验；任何跨层约束优先落在这里。
- `adapters/`、`ingestion/`、`market/`：供应商接入、标准化、复权、披露与行情；不得把供应商字段泄漏到核心领域模型。
- `features/`、`agents/`、`scoring/`、`quant/`：特征、结构化 Agent、确定性评分、预测验证。Agent 只输出经 Pydantic 校验的子分和证据，Manager 只归纳结论。
- `universe/`、`trading/`、`portfolio/`、`backtest/`：可交易池、A 股规则/成交、组合约束、事件驱动回测。涉及交易语义时应连同相关规则和回测测试评估。
- `storage/`、`reports/`、`observability/`、`orchestration/`、`api/`、`search/`：存储/审计、报告、调度 Worker、HTTP API 与检索。异步任务修改须同时检查 API 请求、Worker 消费和运行/审计记录。
- `web/src/`：登录、仪表盘、行情、研究、候选、组合、报告、运行、回测、管理和财报检索界面；前端 API 访问集中在 `web/src/api.ts`，共享类型集中在 `web/src/types.ts`。
- `tests/unit/` 覆盖领域单元测试，`tests/integration/` 覆盖外部服务链路。新增或修复行为时，将测试放在对应层级，避免把领域规则只留在 UI 测试中。

## 不可突破的业务与数据约束

- 所有研究、特征、评分、组合和回测数据必须可按 `symbol + trading_date + available_at` 追溯；每次决策必须显式传入 `decision_at`，并拒绝 `available_at > decision_at` 的未来信息。
- 分析只能读取已提交的不可变 Manifest 和快照。原始载荷、Parquet、模型和报告需保留来源、抓取时间、版本和 SHA-256；DuckDB 不得绕过 Manifest 读取未提交文件。
- 最终评分只可由可复现、版本化的确定性公式生成。当前首版权重、组合限额、熔断、容量阈值和基准配置以 `configs/first_release.v1.json` 为准，生产运行必须记录配置哈希。
- 涨跌停、T+1、停复牌、申报单位、费用、新股阶段和市场差异必须来自**带生效日期**的规则数据。规则缺失、冲突或无法匹配时，宁可拒绝交易/仅发布观察报告，也不得硬编码单一比例或猜测放行。
- 组合与回测保持闭环：目标持仓、单股/行业/换手约束、事件风险、费用、基准、复现哈希和最大回撤熔断必须通过领域模型和回测验证，不得仅在页面展示层实现。

## 变更要求

- 保持分层依赖：API、Worker 和前端调用领域服务；不要在路由、React 页面或供应商适配器中复制评分、风控或交易规则。
- 改动公开 API、Pydantic 模型、任务载荷、数据库表或规则配置时，同步更新调用方、迁移、类型、测试及必要文档；保持向后兼容或明确提供迁移路径。
- 数据库结构变更通过 Alembic migration 交付，不修改已有迁移来伪造历史。数据迁移应可重跑，并明确其锁表、回滚与大表风险。
- 新配置应有版本、校验和默认拒绝策略；不把业务常量散落在代码中。不要改变已版本化配置的既有含义。
- 前端变更须维持认证、HttpOnly 会话、CSRF 双提交和主题初始化行为；不要把 Token、密码或内部错误详情存入 `localStorage`、日志或页面。
- 交付代码必须无已知报错，并保持可维护性：职责清晰、命名准确、避免重复与隐式副作用；新增或修改行为必须有相称的测试、静态检查和可读的失败处理。

## 手机客户端 API 预留与演进

- 现有 `/api/v1` 是 Web 与未来手机客户端共用的公开业务契约；新增手机客户端能力优先复用该版本下的资源接口和 Pydantic 请求/响应模型，不建立绕过领域服务、直接访问数据库的“App 专用后门”。
- 当前认证已区分 Web Cookie 会话与 `Authorization: Bearer` 的 `APP` 会话。手机客户端仅使用 Bearer access token 与 refresh token 流程：登录/换取令牌使用 `POST /api/v1/auth/token`，续期使用 `POST /api/v1/auth/refresh`，退出或设备撤销使用 `POST /api/v1/auth/revoke`。不得要求原生客户端模拟 Cookie、CSRF，也不得为 App 关闭既有 Web 的 CSRF 校验。
- 移动端不得把密码、access token、refresh token、完整报告或敏感审计数据写入普通偏好设置、日志、崩溃上报或剪贴板；令牌仅可放入操作系统安全存储。服务端日志、OpenAPI 示例、测试夹具和错误响应同样不得回显令牌、密码或认证头。
- 所有面向 App 的接口保持 `/api/v1` 的向后兼容：字段只能新增且须有安全默认值；不得重命名、删除或改变既有字段语义、枚举含义、状态码与排序规则。确需破坏性变更时，新建 `/api/v2` 路由、模型、契约测试和明确迁移/弃用窗口，旧版本继续按承诺运行。
- API 响应应使用稳定、客户端友好的 JSON 契约：时间使用带时区的 ISO 8601，日期使用 `YYYY-MM-DD`，金额/价格/比例保持明确单位与精度，股票代码采用系统标准代码。不要向客户端泄漏 ORM 对象、供应商原始字段、内部异常栈、文件路径、存储桶地址或未脱敏的审计载荷。
- 列表和时间序列接口新增或调整时必须定义稳定排序、`limit` 上限和可续页的 cursor/分页契约；不得依赖数据库自然顺序，也不得让移动端为获得完整集合而无限制拉取。响应应只返回当前页面渲染所需字段；大报告、谱系、审计和 K 线明细保持按需获取。
- 会产生副作用或提交异步任务的 App 请求（研究、回测、报告交易方案、预取等）必须支持 `Idempotency-Key`，以“用户 + 路由 + 键 + 请求体哈希”去重，并在重试时返回首次提交的资源或明确冲突。不得因弱网重试创建重复运行、重复方案或重复外部调用。
- 耗时任务统一返回 `202 Accepted` 与可查询的资源 ID/状态地址；手机客户端通过既有运行、研究和回测查询接口轮询或后续明确版本化的推送通道获取状态，不能依赖长时间 HTTP 挂起。取消操作必须是幂等的，并保留运行与审计记录。
- 为移动网络设计接口时，优先提供摘要、按 ID 详情和增量刷新能力；使用合理缓存头、ETag/`If-None-Match` 或版本/更新时间字段。缓存不得绕过用户权限、PIT `decision_at` 校验、Manifest 不可变性或报告版本边界。
- 所有 App 路由继续执行 `require_auth`、用户角色/资源归属校验、限流和统一错误格式；客户端传入的用户 ID、权限、运行状态、评分、交易参数、`decision_at`、Manifest/配置哈希均不可信，必须由服务端校验或派生。任何交易建议仍只适用于模拟组合，不得扩展为自动实盘下单接口。
- 修改认证、公开 API、异步任务载荷或响应模型时，同步更新 OpenAPI、`README.md` 接口清单、Web 调用方、客户端契约文档和测试。至少覆盖：Bearer 鉴权、令牌续期/撤销、401/403/422/429、分页与空结果、幂等重试、任务状态以及旧客户端兼容性；为将来 iOS/Android 客户端预留与 Web 无关的 API 测试夹具。
- API 契约文档统一维护在 `docs/API.md`；新增、删除或改变公开端点、请求字段、响应字段、状态码、排序或权限时，必须同步更新该文档，并用代码实际注册的 FastAPI 路由核对文档端点清单。

## 协作与文件所有权

- 主编排 Agent 负责任务拆分、整合和验收。定制 Subagent 仅在主编排明确授权且确有独立子任务时，最多派发一个直接子代理；该子代理不得继续派发。
- 代码任务开始前，主编排先读取相关路径、识别依赖并划定唯一文件 owner。简单单文件修改、配置调整、格式整理、状态查询和已有测试执行不启用 Subagent。
- 默认最多七个 Subagent 并行；同一目录或文件只有一个 owner。实现稳定后再启动只读审查。
- 每项任务一次主执行、至多一次定向修复。失败日志优先交回原执行者，不派多个 Agent 竞速。
- 架构、A 股规则、风控、因子有效性、数据审计和安全审查使用 `gpt-5.6-sol` `xhigh`；跨模块核心代码和测试工程使用 `gpt-5.6-sol` `high`；常规模块代码使用 `gpt-5.6-sol` `medium`；扫描定位使用 `gpt-5.6-terra` `xhigh`；机械整理使用 `gpt-5.6-luna` `xhigh`。`gpt-5.6-sol`、`gpt-5.6-terra` 和 `gpt-5.6-luna` 最高均可使用 `xhigh`，禁止 `max`。

## 本地运行、验证与安全

- 每次更新代码后，交付前必须执行 `docker compose -p ashare-ai-src -f compose.yaml up -d --build`，将最新镜像重新构建并部署到本机 Docker Desktop；随后至少运行 `docker compose -p ashare-ai-src -f compose.yaml ps`，并检查 Web、API、PostgreSQL、Redis 和 Worker 的健康状态。若受环境或权限限制无法完成，必须在交付说明中明确列出原因和未验证项。使用显式项目名是强制要求，避免当前目录名、临时 ASCII 构建目录和旧 Compose 配置创建多个互不相同的项目。
- 本地完整链路：基础依赖由 `docker compose -p ashare-ai-src -f compose.yaml up -d postgres redis` 提供；当前默认低内存栈不再捆绑 MinIO，使用 `object-data` 卷或外部 HTTPS S3。随后运行 `ashare-ai doctor`、`ashare-ai migrate`，再启动 API 和串行 `job-worker`；不要在 Docker 基础服务启动后又在容器内重复启动 API/Worker。
- Docker 故障恢复必须先检查同一 Compose 项目的依赖：`docker compose -p ashare-ai-src -f compose.yaml ps -a`、`docker compose ls --all` 和 `docker ps -a`。若 API 日志出现 `failed to resolve host 'postgres'`，或 Worker 日志出现 `failed to resolve host 'redis'`，先启动对应的 `postgres`、`redis` 并等待其 healthcheck 通过，再启动 API/Worker；不得通过反复重启应用容器掩盖依赖未运行。`Exited (0)` 且日志含 `SIGTERM` 表示容器被正常停止，不等同于 OOM 或应用崩溃；恢复时不得删除数据库、Redis、lake 或 object 数据卷。
- 前端开发使用 `cd web; npm ci; npm run dev`，默认访问 `http://localhost:5173`，并把 `/api` 代理到 `http://127.0.0.1:8000`。完整 Docker 栈由 Nginx 在 `http://localhost` 暴露网页。
- Python 修改至少运行受影响的 `pytest` 用例；跨层、数据或交易语义修改运行完整 `\.venv\Scripts\python -m pytest`（环境允许时）。前端修改至少运行 `cd web; npm test -- --run`。
- `web/dist` 是**有意入库**的前端构建产物（Linux 原生管理器等直接复用），不是无关生成物。改动 `web/src/` 或 `web/` 构建配置时，pre-commit 钩子（`.githooks/pre-commit`，`core.hooksPath` 已配置）会自动重建并暂存 `web/dist`；如用 `--no-verify` 跳过钩子，提交前必须手动执行 `cd web && npm run build` 并一并提交 `web/dist`，否则 Linux 原生版等会拿到过期前端。
- 提交前执行相应范围的 Ruff 与 mypy；不能运行的集成检查需说明原因，不能以跳过测试替代验证。
- 不把真实密码、Token、API Key、cookie、个人数据或生产数据写入 Git、日志、测试夹具、截图或任何 `*.example` 文件。真实凭据仅位于未跟踪的 `.env`；提交前检查差异中没有凭据和无关生成物（`web/dist` 是有意入库的构建产物，除外）。
- 避免破坏现有工作区改动；不要使用破坏性 Git 命令或清理数据目录。涉及删除数据、迁移生产库、触发任务或外部供应商调用时，先确认用户授权。
