# ashare-ai-research 渐进式性能与跨平台实施计划

## 1. 结论

原方案的总体方向（列式数据、DuckDB、独立计算 Worker、跨平台构建）可行，但不能按原文直接实施：

- 本仓库已经使用 Parquet、PyArrow、DuckDB、Redis 和串行/双 Worker；“从零引入”会重复建设。
- 当前强制运行时仍是 Python 3.11+，Pandas 只在 Qlib 可选网关中使用，不能假设所有 Pipeline 都是 Pandas 热点。
- Prefect 已是可选能力，不能把生产任务改成 Prefect/Celery 微服务作为前置条件。
- Rust/PyO3 会引入 ABI、wheel 发布、调试和数值一致性成本，必须用基准证明收益后再进入核心路径。
- “内存降低 40%~60%、速度提升 3x~10x、每阶段若干周”只能作为待验证假设，不能作为交付承诺。

因此采用兼容优先、可回滚、以基准门禁驱动的增量路线。现有 API、Manifest/PIT 规则、交易规则和 Worker 契约保持不变。

## 2. 目标与验收口径

### 2.1 首期目标（必须完成）

1. 所有大快照读取都能选择 Arrow batch 流式路径，不因兼容 API 被迫一次性 `fetchall()`。
2. 保持 `symbol + trading_date + available_at`、Manifest 校验和 SHA-256 校验不变。
3. 建立可重复的基线和回归基准：峰值 RSS、运行时、行数、输出哈希和数值误差。
4. Windows、Linux、macOS 的路径处理只使用 `pathlib.Path`/`PathBuf`；CI 至少覆盖 Python 单测和 wheel 构建。

### 2.2 后续目标（通过门禁后再做）

- 对确认的计算热点逐个提供可选加速后端；没有基准证据的模块不改写。
- Rust/PyO3 仅作为可选 wheel 后端，Python 实现始终保留，支持按配置回退。
- 性能目标改为相对基线：单项优化至少降低 20% 峰值 RSS 或提升 1.5x；累计目标再根据实测调整。

## 3. 当前实施状态

已完成第一项低风险改造：`ImmutableLake` 新增 `query_batches()` 和 `query_arrow()`；并开始第二阶段的最小 Rust 垂直切片。

- `query_batches()` 在读取前复用现有 committed Manifest、文件存在性和 SHA-256 校验，并以 Arrow `RecordBatch` 迭代返回结果。
- 现有 `query()` 保持原有 `list[dict]` 返回契约，内部改用 batch 路径，旧调用方无需迁移。
- 单元测试覆盖批大小、排序、空结果和非法批大小；后续调用方可逐步迁移到流式 API。
- `native/ashare_ai_core` 提供技术指标 Rust kernel；Python 保留 PIT 过滤和参考实现，通过 `ASHARE_NATIVE_TECHNICAL=auto|on|off` 显式选择或回退。
- 已在 Windows + Python 3.11 上完成 PyO3 `abi3` wheel 的本地构建和真实导入验证；尚未宣称性能收益，需阶段基准后再决定默认启用。
- 长期 API/Worker 进程已增加带 RSS 门槛和冷却时间的自动内存回收：清理领域缓存后执行 full GC，Linux/glibc 额外执行 `malloc_trim`；重任务仍以一次性子进程退出作为最可靠的堆回收边界。

## 4. 分阶段路线

### 阶段 0：基线与边界（1 周）

- 盘点 `features/`、`quant/`、`backtest/`、`storage/` 的真实数据量和热点。
- 为代表性快照记录峰值 RSS、耗时、输出哈希和数值基准；固定 Python、依赖锁和数据 Manifest。
- 产出 `docs/performance-baseline.md`，明确每个基准的命令、输入和容差。

出口：没有基线或无法重放的任务不得进入后续重写。

### 阶段 1：列式读取和内存控制（1~2 周）

- 优先把大结果消费方迁移到 `ImmutableLake.query_batches()`；只在需要随机访问时使用 `query_arrow()`。
- 对 PyArrow/ DuckDB 查询增加列投影、过滤条件和批大小参数，禁止绕过 Manifest 直接读取路径。
- 仅在等价性测试通过后，替换局部 Pandas 物化；Qlib 网关继续保留 Pandas 边界。

出口：代表性任务峰值 RSS 相对基线下降至少 20%，输出哈希和 PIT 测试全部通过；失败则回滚调用方迁移，不回滚 Manifest 校验。

### 阶段 2：可选计算加速后端（2~4 周，按热点数量）

- 先用 NumPy/Arrow kernel 或 Polars 对单个技术指标/横截面算子做等价实现，保留 Python 参考实现。
- 只有当基准达到 1.5x 以上且误差在预设 `assert_allclose` 容差内，才评估 Rust/PyO3。
- Rust crate 放在独立 `native/ashare_ai_core`，通过小型、版本化的 Python facade 接入；默认仍走 Python，配置显式启用加速。
- wheel 使用 GitHub Actions 构建 Windows MSVC、Linux glibc 和 macOS 双架构；构建失败不得影响纯 Python 安装。

出口：每个算子有 Python/Rust 双轨测试、基准报告和回退开关；没有收益的算子不迁移。

### 阶段 3：任务隔离与容量验证（1~2 周）

- 复用现有 Redis 队列和 `research_worker`/`backtest_worker`，把重计算放到已有 Worker 进程；不新增 Celery 集群。
- Prefect 仅作为显式配置的编排选项，失败时回退现有调度路径。
- 增加任务超时、取消、资源上限和审计记录测试，确保 API 只提交任务并返回可轮询状态。

出口：API 在后台重任务期间健康检查和轻量请求保持可用，任务重试具有幂等性。

### 阶段 4：跨平台 CI 与发布（持续）

- CI 矩阵覆盖 Python 3.11/3.12、Windows、Ubuntu、macOS；先跑纯 Python 测试，再构建可选 wheel。
- 对路径、时区、文件锁、SQLite/DuckDB 读写增加平台测试。
- 每次发布记录代码 SHA、依赖锁 SHA、配置 SHA 和 wheel SHA-256；提供回滚到纯 Python 版本的说明。

## 5. 风险与拒绝条件

- 数值差异：以领域容差和输出哈希双重校验；交易金额、费用、规则计算禁止静默近似。
- ABI/编译失败：wheel 为可选产物，缺失时安装和运行必须继续使用 Python 实现。
- 内存收益不稳定：按固定数据 Manifest 重复三次取中位数；未达到门禁不合并重写。
- PIT/审计回归：任何优化若改变 `available_at > decision_at` 的拒绝行为，立即拒绝发布。
- 资源不足：不删除数据库、Redis、lake 或 object 卷；Docker 验收遵循仓库 `AGENTS.md` 的显式项目名和健康检查要求。

## 6. 交付物

- `plan.md`：本计划及每阶段基线/出口记录。
- `src/ashare_ai/storage/lake.py`：Manifest 安全的 Arrow 流式查询 API（已实现）。
- `tests/unit/test_storage.py`：流式查询回归测试（已实现）。
- `docs/performance-baseline.md`、基准脚本、CI wheel 工作流（阶段 0/4 交付）。
- 每个后端迁移对应的双轨数值测试、性能报告和回退配置。
