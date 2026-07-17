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

## 协作与文件所有权

- 主编排 Agent 负责任务拆分、整合和验收。定制 Subagent 仅在主编排明确授权且确有独立子任务时，最多派发一个直接子代理；该子代理不得继续派发。
- 代码任务开始前，主编排先读取相关路径、识别依赖并划定唯一文件 owner。简单单文件修改、配置调整、格式整理、状态查询和已有测试执行不启用 Subagent。
- 默认最多七个 Subagent 并行；同一目录或文件只有一个 owner。实现稳定后再启动只读审查。
- 每项任务一次主执行、至多一次定向修复。失败日志优先交回原执行者，不派多个 Agent 竞速。
- 架构、A 股规则、风控、因子有效性、数据审计和安全审查使用 `gpt-5.6-sol` `xhigh`；跨模块核心代码和测试工程使用 `gpt-5.6-sol` `high`；常规模块代码使用 `gpt-5.6-sol` `medium`；扫描定位使用 `gpt-5.6-terra` `xhigh`；机械整理使用 `gpt-5.6-luna` `xhigh`。`gpt-5.6-sol`、`gpt-5.6-terra` 和 `gpt-5.6-luna` 最高均可使用 `xhigh`，禁止 `max`。

## 本地运行、验证与安全

- 本地完整链路：基础依赖由 `docker compose up -d postgres redis minio minio-init` 提供；随后运行 `ashare-ai doctor`、`ashare-ai migrate`，再分别启动 API、Research Worker、Backtest Worker 和（如需）调度器。不要在 Docker 基础服务启动后又在容器内重复启动 API/Worker。
- 前端开发使用 `cd web; npm ci; npm run dev`，默认访问 `http://localhost:5173`，并把 `/api` 代理到 `http://127.0.0.1:8000`。完整 Docker 栈由 Nginx 在 `http://localhost` 暴露网页。
- Python 修改至少运行受影响的 `pytest` 用例；跨层、数据或交易语义修改运行完整 `\.venv\Scripts\python -m pytest`（环境允许时）。前端修改至少运行 `cd web; npm test -- --run`，并在交付前运行 `npm run build`。
- 提交前执行相应范围的 Ruff 与 mypy；不能运行的集成检查需说明原因，不能以跳过测试替代验证。
- 不把真实密码、Token、API Key、cookie、个人数据或生产数据写入 Git、日志、测试夹具、截图或任何 `*.example` 文件。真实凭据仅位于未跟踪的 `.env`；提交前检查差异中没有凭据和无关生成物。
- 避免破坏现有工作区改动；不要使用破坏性 Git 命令或清理数据目录。涉及删除数据、迁移生产库、触发任务或外部供应商调用时，先确认用户授权。
