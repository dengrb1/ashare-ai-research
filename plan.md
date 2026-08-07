# AshareAI 剩余阶段执行交接单（交付 Codex）

> 这是一份**执行工作单**，不是策略文档。按 §4 任务编号顺序执行；每个任务都有明确的
> 「目标 / 改动文件 / 做法 / 完成定义」。提交前按 §5 自检，完成后按 §7 报告。
> 严格遵守 §2 契约红线和 §6 禁止事项。当前代码在分支 `refactor/memory-architecture-control`。

## 1. 系统一句话

收盘后 A 股 AI 研究系统：数据入湖（Parquet/DuckDB）→ PIT 校验 → 特征/Agent → 确定性评分 →
模拟组合/回测 → 报告。核心价值是**可复现**（`output_hash`/`stable_hash` 逐字节一致、PIT 不变量）。
运行形态：Docker Compose 或 Windows/Linux 原生（FastAPI + 多个 worker + Redis 租约队列 + PostgreSQL）。

## 2. 契约红线（任何改动不得违反）

1. **确定性**：`output_hash` / `stable_hash` / 舍入语义**逐字节不变**。钱已被 `_round_money`/`_money`
   量化到 quantum（0.01）的整数倍 → 整数分表示是精确的；任何优化不得改变 rounding。
2. **PIT 不回归**：`available_at <= decision_at` 拒绝行为不变。
3. **API 契约不变**：`/api/v1` 路由、状态码、分页、幂等语义不变。
4. **金值测试先行**：每个改动先写金值测试（固定输入下输出哈希完全一致），再改实现。
5. **性能门禁**：性能/内存以固定数据 Manifest 重复 ≥3 次取中位数，未达门禁不合并。

## 3. 已完成（只读参考，禁止改动/重做）

### 3.1 阶段 2W：数据预热（已实现，未提交）

- `src/ashare_ai/market/warmup.py`（新）：`collect_warmup_symbols`（全体用户自选/持仓并集，有界）
  + `warm_market_if_due`（门控：启用 + 交易时段 + 间隔；后台线程执行）；专用轻量 Sina 行情服务
  （不碰 API 的 provider 槽位、不 spawn AKShare 子进程），预热结果经 Redis 共享给 API。
- `src/ashare_ai/orchestration/serial_worker.py`：`run_loop` 每迭代调用 `warm_market_if_due()`。
- `src/ashare_ai/market/service.py`：`quotes`/`quote`/`klines` 增加 **stale-while-revalidate**——
  有旧缓存（未过 `market_stale_seconds`）立即返回、后台线程以 `force_refresh=True` 刷新；
  按缓存 key 限一个后台线程（`_background_refreshing`）。**行为变化**：上游故障的
  `delayed`/`stale` 标记从"当前请求同步降级"变为"后台刷新后于下一次读取体现"。
- `src/ashare_ai/api/app.py`：`/api/v1/app/bootstrap` 后 `_warm_user_market(user_id)` 按用户去抖
  （`_bootstrap_warm_at`，上限 1024 + 过期清理）后台预取该用户自选/持仓。
- 新增 6 个 `MARKET_WARMUP_*` 配置项（`core/config.py`）：`enabled`(True)、`interval_minutes`(5)、
  `max_symbols`(50)、`index_symbols`("")、`kline_limit`(160)、`debounce_seconds`(300)。
- 测试：`tests/unit/test_market_warmup.py`；`test_web_auth_market.py` 3 个既有测试已按 SWR 语义更新。

### 3.2 既有能力（存在，直接复用）

- `ImmutableLake.query_batches()` / `query_arrow()`，`query()` 内部走批次路径。
- `native/ashare_ai_core` Rust 技术指标 kernel，`ASHARE_NATIVE_TECHNICAL=auto|on|off` 回退。
- 长驻进程 RSS 门槛 + 冷却的自动内存回收（Linux/glibc 额外 `malloc_trim`）。

## 4. 待办任务（按编号顺序执行）

### 任务 1：研究报告耗时压降（用户优先，诊断已完成 → 实施）

**诊断结论（2026-08-08 实测，只读查询 Docker postgres + audit）**：
- 08-06 墙钟 3.01h 拆解：**~2.5h 是 `DATA_READINESS_RETRY`**（每 5 分钟探测、共 ~30 次，
  等 AKShare 基准数据）→ 09:36 才 READY → 实际执行 ~30 分钟且伴随 **`AKSHARE_FETCH_FAILED`
  × 20+（同一秒爆发，限流）** → FUSED。**LLM Agent 只占 3.4 分钟**（9 次调用，prompt 缓存全命中）。
- 08-05 对照：15:10 启动、5.1 分钟完成——数据早到。**AKShare 基准数据到达时间不稳定
  （15:10~17:30），决定了研究是 5 分钟还是 3 小时。**
- **根因**：① 就绪探测每 5 分钟做 3 次串行 AKShare 拉取，30 次重试 ≈ 90 次调用，**探测风暴
  加重限流，形成"越探测越限流、越限流越探测"自愈循环**；② 探测通过后实际取数仍被限流
  （`AKSHARE_FETCH_FAILED` 风暴）。**Agent 并发不是瓶颈，不要在此浪费时间。**
- 诊断脚本（只读，可复跑验证）：`docs/performance/research_time_diagnose.py --env .env`。

**目标**：数据早到时 ≤5 分钟；数据晚到时"到位后 ≤10 分钟"；消除探测风暴与取数失败风暴。
**红线**：`output_hash`/评分/报告内容不变；不改推理档位。

**改动文件与做法**：
1. **就绪探测瘦身**（`src/ashare_ai/orchestration/research_schedule.py`，`_probe_benchmarks`、
   `_READINESS_BUDGET_*`、`_readiness_cached`）：3 个指数（000300/000905/000852）**一次批量
   拉取**替代 3 次串行；探测结果缓存 TTL 拉长；**限流/超时时自适应退避**（重试间隔按指数增长，
   不再固定 5 分钟硬顶）。
2. **取数失败风暴**（`src/ashare_ai/orchestration/builtin.py` ~1838-1943 取数路径）：对
   429/超时类错误退避更久（`akshare_fetch_max_attempts`/`akshare_fetch_backoff_seconds`），
   失败走备用源；消除同一秒内 20+ 次重试。
3. **（可选，需用户确认）研究启动时间**：`daily_research_start_hour`（默认 15）→ 16:30/17:00，
   让探测通常立即通过、报告反而更早出。

**完成定义**：
- 复跑 `research_time_diagnose.py`：`DATA_READINESS_RETRY` 次数降到 ≤3（不再探测风暴）；
  数据到位后执行 ≤10 分钟；`AKSHARE_FETCH_FAILED` 不再同一秒爆发。
- `pytest tests/unit`、`ruff check .`、`mypy src` 全绿；研究相关金值测试 `output_hash` 不变。
- 若执行段已 ≤5 分钟仍想再压，再考虑 `llm_agent_max_concurrency` 4→8（输出不变）或任务 3/4。

### 任务 2：阶段 0 测量热点基线（已授权，前置门禁）

- **目标**：把结构推断换成实测热点分布，校准后续倍数。
- **改动文件**：新增 `docs/performance/hotspot-baseline-<date>.md`；必要时新增 `docs/performance/bench_*.py`。
- **做法**：
  1. 运行固定输入任务（不触发真实 LLM）：确定性研究（模型关闭/模拟客户端）、固定已提交快照回测、
     固定输入交易方案生成。
  2. 从 audit `STAGE_COMPLETED.details.duration_ms`（已埋点）读各阶段耗时。
  3. 记录每任务峰值 RSS、`output_hash`、阶段耗时分布。
  4. 重点量化：交易方案生成占墙钟比例、DuckDB 查询峰值、每任务子进程启动成本占比。
- **完成定义**：产出 `hotspot-baseline-<date>.md`，含每个基准的命令/输入/容差；没有基线不得开工重写。

### 任务 3：阶段 1 交易方案热路径整数分（预计收益最大）

- **目标**：把交易方案生成墙钟压掉一个数量级（结构估计 ~1000 万次 Decimal 内层迭代/job）。
- **改动文件**：
  - `src/ashare_ai/trading/execution.py`（`_execute_one`、`_round_money`、`execute_orders`）
  - `src/ashare_ai/backtest/trade_plan.py`（`_simulate_one_entry`、`_simulate_parameter_set`、`_money`）
  - 新增金值测试 `tests/unit/test_trade_plan_integer_cents.py`
- **做法**：
  1. 热路径钱改**整数分**：`Decimal` → quantum 缩放 int（2dp → 分），`_round_money`/`_money` 改整数对齐。
  2. **浮点路径原样保留**：volatility、participation、`_slippage_bps` 的 `float → sqrt → Decimal(str())`
     不动，保证哈希。
  3. 单订单执行快速路径：`execute_orders` 每调用都 `sorted()`（execution.py:188），优化器每层只传 1 单 → 短路。
  4. 消每迭代 Pydantic 构造：`bar.model_copy(update=...)`、`Order(...)`、`AccountState(cash=...)`
     （trade_plan.py:520-541）改热路径内复用/轻量结构，降低 GC 压力。
- **完成定义**：
  - 固定输入 `output_hash` 逐字节一致（金值测试覆盖全部策略网格组合：3×3×3×2×4）。
  - 交易方案生成墙钟 **≥5×**（相对任务 2 基线）。
  - `pytest tests/unit`、`ruff check .`、`mypy src` 全绿。

### 任务 4：阶段 2 回测引擎预分区

- **目标**：消除 O(天×N) 二次方扫描。
- **改动文件**：`src/ashare_ai/backtest/engine.py`（`run` 主循环 230-285 行、`_plan_orders`、`_market_value`）。
- **做法**：把 bars/rules/adv/volatilities 按日期分组一次，替代每交易日对全量映射扫描。
- **完成定义**：固定输入回测 `output_hash` 一致；回测墙钟下降（相对任务 2 基线）。

### 任务 5：阶段 3 DuckDB / lake 内存控制

- **目标**：堵住 Worker 子进程瞬时 RSS 膨胀。
- **改动文件**：`src/ashare_ai/storage/lake.py`。
- **做法**：
  1. `duckdb.connect(":memory:")`（lake.py:127）加 `PRAGMA memory_limit`（默认 256 MiB，环境变量可调）。
  2. 热路径大结果消费方迁移到既有 `query_batches()`；`query_arrow()` 仅用于随机访问。
  3. `write_snapshot`（lake.py:38-39）**流式哈希**，移除 `list(rows)` 全量物化。
- **完成定义**：DuckDB 查询峰值 RSS 不超过显式上限；行数、`payload_sha256`、`parquet_file_sha256` 不变。

### 任务 6：阶段 4 导入面瘦身 + searxng 门控

- **目标**：api 工作集 ~137 MiB 再降 20-60 MiB；searxng 门控省 ~104 MiB（栈内最大单一内存户）。
- **改动文件**：`src/ashare_ai/api/app.py`、`compose.yaml`、搜索降级相关。
- **做法**：
  1. **导入面瘦身（DO，不依赖决策）**：重模块懒加载，沿用 `read_backtest_bundle` 范例（app.py:771）。
  2. **searxng 门控（依赖决策 2）**：profile 门控 + 搜索降级（`web_search_available=bool(searxng_base_url)`
     已建模"有无"两种世界）。
- **完成定义**：api 工作集下降 ≥20 MiB（测量确认）；searxng 缺位时搜索优雅降级、服务不依赖其健康。

### 任务 7：阶段 5 Rust 三件（依赖测量 + 决策 3）

- **门禁**：只做任务 2 数据证明占比显著的部分；特征 kernel 必须**胜过 Arrow/DuckDB 向量化等价实现**才做。
- **候选**：
  1. **IPC orjson**（`akshare_worker.py` + `market/service.py`）：双端同改、内部协议、无字节一致性要求。
     门禁 = 端到端中位下降 ≥10%。
  2. **特征提取/股票池原生 kernel**（`technical.py`/`fundamental.py`/`builder.py` 的 PIT 过滤 + 去重 +
     对齐 + 归约）：需决策 3；PIT 语义仍由 Python 校验器保证。
  3. **纯数值 kernel**：`calculate_performance_metrics`（`metrics.py`）+ 交易方案 returns 归约尾巴
     （`trade_plan.py:469-485`）：顺手做，无收益不迁移。
- 全部遵循既有 `auto|on|off` 回退与 wheel 构建纪律。
- **完成定义**：每个算子有 Python/Rust 双轨测试、基准报告、回退开关；无收益不迁移。

## 5. 自检流程（每个任务提交前）

```powershell
.\.venv\Scripts\python -m pytest tests/unit -q
.\.venv\Scripts\ruff check .
.\.venv\Scripts\python -m mypy src
```

金值测试用固定输入；输出哈希必须逐字节一致。

## 6. 禁止事项（明确不做，别碰）

| 禁止 | 原因 |
|---|---|
| 引入 Axum / 其他语言微服务 | 与目标无关，破坏进程内不变量 |
| 交易优化器/回测执行 **Rust 化** | 状态化领域仿真，`output_hash` 跨运行时浮点风险 |
| 评分公式、`stable_hash` 内核化 | 标量 O(1)，确定性是核心价值 |
| 下调容器 `mem_limit` | 无压力数据支撑，余量不足以判安全 |
| orjson 端到端（无基准） | 序列化占比未证明；先过任务 1 再评估 |
| 改动已完成阶段 2W 的任何文件 | 见 §3.1，已有测试覆盖 |

## 7. 报告格式（每个任务完成时输出）

- 改了什么（文件 + 关键函数）
- 测试结果（命令输出摘要）
- 相对基线的性能/内存数据
- 遗留问题 / 需要用户决策的点

## 8. 需要用户拍板的决策

1. **决策 2（searxng）**：门控降级（推荐，省 ~104 MiB）vs 保持现状——任务 5.2 前确认。
2. **决策 3（特征计算）**：Rust kernel（契约扩展）vs Arrow 向量化（零风险）——任务 6 前确认。
3. 任务 1 测量需真实 AKShare/固定输入运行环境；预热调优需少量真实行情读取（已单独授权）。
