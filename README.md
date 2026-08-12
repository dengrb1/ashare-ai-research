# A 股 AI 投研系统

这是一个面向 A 股研究、评分、模拟组合和事件回测的系统。它提供 React Web、FastAPI 后端、Docker Compose 和 Windows/Linux 原生运行方式；不连接自动实盘下单，也不构成投资建议。

## 能力

- 收盘后研究、候选池、确定性评分、研究报告、回测和模拟组合。
- Web 工作台、AI 问答、可审计的研究快照与 API。
- 沪深 300（000300）、中证 500（000905）、中证 1000（000852）大盘指数。
- 指数实时行情通过 `/api/v1/market/indices` 展示；研究任务使用 `benchmark_returns` 在任务决策日生成冻结指数快照。
- 市场环境按 1/5/20 个交易日收益识别为 `RISK_ON`、`NEUTRAL`、`RISK_OFF` 或 `UNKNOWN`，并参与评分调整与风险乘数。
- 旧公式和旧报告没有指数快照时按中性环境兼容，不会被当前实时行情改写。

## 启动

```powershell
Copy-Item .env.local.example .env
Copy-Item .env.docker.example .env.docker
# 在未跟踪的 .env/.env.docker 中填写密码、数据库、Redis 和模型配置
docker compose -p ashare-ai-src -f compose.yaml up -d --build
```

打开 `http://localhost`。默认 Compose 不启动可选的 SearXNG；需要检索时使用 `--profile search`。Docker 与原生部署共享同一 FastAPI、评分配置和 Alembic 迁移，不存在两套指数评分实现。

## AI 配置

Web 管理员在模型设置中配置 OpenAI-compatible Base URL、模型、API Key、Organization/Project 和超时。后端负责加密、版本、探测、启用和审计；API Key 不写入前端本地存储、日志或报告。AI 只能解释已有确定性结果，不能修改评分、风险门槛或指数快照。

## 评分与报告

当前市场公式为 `composite-35-35-20-10-dividend-news-market-v3`：基本面 35%、技术面 35%、事件/情绪 20%、质量置信度 10%，并叠加分红、事件风险和冻结大盘环境。报告会显示三指数收益、市场状态、评分调整和风险乘数；个股详情显示对应的大盘字段。

## 开发与验证

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest
cd web
npm test -- --run
npm run build
```

常驻进程默认使用 `LIGHTWEIGHT` 模式，行情和搜索按需启动；不要在应用启动时预热 AKShare、量化模型或非必要 Worker。生成的 `build/`、密钥、`.env` 和 APK 不提交。

## 目录

`src/ashare_ai/` 后端与评分；`web/` React/Vite；`configs/` 版本化配置；`migrations/` 数据库迁移；`docker/` 容器与 Edge Gateway；`docs/` 部署与 API 文档；`tests/` 测试。
