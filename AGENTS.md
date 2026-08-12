# 仓库协作说明

## 范围

本仓库包含 FastAPI、PostgreSQL/Redis、Parquet/DuckDB、研究 Worker 和 React/Vite Web。系统只支持研究、回测和模拟组合，不实现自动实盘交易。

## 数据与评分约束

- 所有研究结果必须关联 `symbol`、`trading_date`、`available_at` 和 `decision_at`，拒绝未来证据。
- 评分只能由版本化确定性公式生成，Agent 只能提供经过 Pydantic 校验的子分和证据。
- 大盘指数只从冻结的 `CanonicalDailyBundle.benchmark_returns` 生成研究快照，禁止以打开报告时的实时行情回写评分。
- 指数快照包含沪深 300、中证 500、中证 1000 的 1/5/20 日收益、市场状态、评分调整和风险乘数；旧版本缺失字段按中性处理。
- Docker、Windows 原生和 Linux 原生共享同一配置、API、评分代码和 Alembic 迁移。

## API、Web 与安全

- `/api/v1` 是 Web 和移动端共用契约；新增字段必须有安全默认，破坏性变化才使用新版本。
- `/api/v1/market/indices` 是实时展示接口，与已冻结研究快照隔离。
- AI 设置由后端加密、校验、探测和审计；不要在前端、日志、测试夹具或数据库明文保存 API Key。
- 保持认证、CSRF、限流、幂等和审计边界；不要在路由或页面复制评分与风控逻辑。

## 低内存与生命周期

默认使用 `LIGHTWEIGHT` 运行模式。可选的搜索、行情子进程、量化模型和 Worker 按需启动，任务完成后释放；避免全局轮询、启动预热和无界缓存。修改运行时回收策略时同步更新 Docker/原生配置与测试。

## 目录与验证

后端代码在 `src/ashare_ai/`，前端在 `web/`，配置在 `configs/`，迁移在 `migrations/`，测试在 `tests/`。修改代码后运行：

```powershell
$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest
cd web; npm test -- --run; npm run build
```

提交前运行 `git diff --check`，不得提交 `.env`、凭据、数据库导出、`build/` 或私有网关配置。使用 Conventional Commit 标题。
