# 资源与性能基线：2026-08-06

> 本文档对应《A 股 AI 研究系统：低资源、高性能与中文错误实施方案》Phase 0 的基线产物
> （`docs/performance/resource-baseline-YYYYMMDD.md`）。后续每个候选改动都以本表为准，
> 并写明 `DO / SKIP` 与数据依据。

## 0.1 环境与工作负载标识

- 代码 SHA（基线）：`27fb6b4`（提交前基线）；本分支：`feat/chinese-errors-resource-optimization`
- 宿主：Windows 11 Pro（10.0.26100），Docker 29.6.1，Compose v5.3.0（Docker Desktop VM）
- 拓扑：默认 `SERIAL + LIGHTWEIGHT`（`docker compose -p ashare-ai-src -f compose.yaml`）
- 运行中的服务：`web`、`api`、`postgres`、`redis`、`job-worker`、`exit-advice-worker`、
  `searxng`、`edge-gateway`（全部 healthy，容器 `Up 4 hours`）
- 系统配置哈希 / 模型配置哈希：本次未变更任何版本化配置，沿用既有 `configs/first_release.v1.json` 与
  持久化配置版本（未授权不读取生产配置哈希进行改写，仅记录“未变更”）
- 运行模式：API 默认 `LIGHTWEIGHT`；Worker 在 SERIAL 拓扑下为轻量轮询器，重任务通过
  `isolated_job.execute_isolated()` 子进程执行

### 已测工作负载（本次实际执行）

1. 启动后静置（≥4 小时）后的空闲态：`docker stats` 单次 + 5 次连续 1 秒采样；
   再用 cgroup v2 `memory.current / memory.stat / pids.current` 交叉核验工作集与进程数。
2. 只读探测：API 进程冷导入 `ashare_ai.api.app` 的 import 耗时（`python -X importtime`）。
3. AKShare IPC 序列化微基准（stdlib `json` vs `orjson`，三种协议形态）。

### 未测工作负载（需要授权，本次明确未触发）

以下每一项都需要真实供应商 / LLM / 新研究任务，本次**未执行**（不触发真实外部调用）：

1. 确定性研究流程（模型关闭或模拟客户端）的固定 canonical bundle 任务。
2. 固定已提交快照的回测任务。
3. `exit-advice-worker` 固定输入任务。
4. 真实 AKShare / 行情源与真实 LLM 场景（单独报告网络延迟与限流）。
5. Worker 连续 5 个隔离任务后的回落测试。

## 0.2 空闲基线指标（实测）

`docker stats` 连续 5 次 1 秒采样（空闲稳定区间），再取 cgroup 交叉核验（MiB）：

| 容器 | 内存上限 | docker stats 报告 | cgroup current | anon | file | inactive_file | 工作集≈(current−inactive_file) | pids |
|---|---|---|---|---|---|---|---|---|
| job-worker | 700 | 306–484（波动） | 434 | 60 | 254 | 124 | **311** | 3 |
| api | 384 | 135–136 | 139 | 120 | 9 | 2 | **137** | 13 |
| exit-advice-worker | 320 | 45–47 | 53 | 33 | 15 | 4 | **48** | 3 |
| web | 32 | 24 | 28 | 16 | 6 | 1 | **27** | 23 |
| postgres | 128 | 41 | 54 | 17 | 30 | 9 | **46** | 14 |
| redis | 64 | 14 | 26 | 5 | 17 | 10 | **17** | 9 |
| searxng | 256 | 114 | 150 | 104 | 35 | 33 | **117** | 11 |

要点：

- **`job-worker` 空闲“报告值”波动大（306–484 MiB），但真实匿名工作集只有 ~60 MiB**，其余为
  从 Parquet/依赖读取产生的可回收页缓存（`inactive_file` 124 MiB）。这与计划的既有结论一致：
  Worker 父进程是轻量轮询器，重任务在隔离子进程；**当前无泄漏迹象，也无 OOM 风险**。
- `api` 匿名工作集 ~120 MiB（上限 384 MiB），余量充足；`web` 工作集 27/32 MiB 偏紧但稳定
  （静态 Nginx，无风险，也不下调）。
- `docker stats` 报告值包含页缓存，**不能单独作为“实际驻留”结论**；本表以 cgroup 工作集为准。

### 只读导入探测

```bash
docker compose -p ashare-ai-src -f compose.yaml exec api python -X importtime -c "import ashare_ai.api.app"
```

结果：`ashare_ai.api.app` 全树累计导入 ~1.07 s（自耗 ~0.12 s；其余为 pydantic.v1 等依赖）。仅用于
定位启动导入成本，不作为常驻内存依据（常驻以稳定后 cgroup 工作集为准）。

### AKShare IPC 序列化微基准（仅序列化，非端到端）

命令（仓库内可复现）：

```bash
.\.venv\Scripts\python docs\performance\bench_ipc_serialization.py
```

| 形态 | stdlib json 中位 | orjson 中位 | 比值 |
|---|---|---|---|
| 小请求（1 条） | 0.005 ms | 0.001 ms | 7.6× |
| 5000 根 K 线 | 9.15 ms | 1.02 ms | 9.0× |
| 接近 8 MiB 上限（43 462 根） | 81.0 ms | 11.6 ms | 7.0× |

**结论：orjson 序列化确实更快（约 7–9×），但这只是序列化单点，不是端到端提速证明。**

## 0.3 阶段耗时观测（已实现）

对 `ApplicationPipeline._stage` 增加最小可观测性：使用单调时钟（`time.perf_counter()`），把
`duration_ms`（整数毫秒，只增字段）写入 `STAGE_COMPLETED` 与 `STAGE_FAILED.details`。
不新增表、不改领域输出或哈希；补了 `tests/unit/test_production_pipeline.py` 断言。

本次未用该字段分析真实阶段耗时——分析真实研究/回测任务需要授权运行，因此只交付观测能力，
不虚构阶段耗时结论。重新采集基线时直接从 audit 的 `duration_ms` 读取即可，不必再用
`created_at` 相邻间隔估算。

## 0.4 Phase 2/3 候选决策（数据依据）

### Phase 2（内存）

| 候选 | 依据 | 决策 |
|---|---|---|
| 下调各容器 `mem_limit` | 未做代表压力测试；仅空闲数据显示余量差异较大，不足以判定安全余量 | **SKIP**（保护上限调整不计入内存收益，且无压力数据支撑） |
| API 重型模块启动驻留 | API 工作集 ~137 MiB、上限 384 MiB；LIGHTWEIGHT 下 AKShare SDK 在隔离子进程，收盘自动回收；无异常驻留证据 | **SKIP**（保留现状） |
| Worker 任务后回落 | 空闲匿名工作集仅 ~60 MiB，父进程无泄漏；但“5 个隔离任务后回落”需授权运行 | **SKIP**（待授权后复测） |
| 缩小行情缓存 / 线程池 | 无压力场景数据，且风险是请求延迟与供应商流量上升 | **SKIP** |

### Phase 3（性能）

| 分支 | 依据 | 决策 |
|---|---|---|
| 3.1 模型网络耗时主导 | 需授权真实模型对比 `llm_agent_max_concurrency` | **SKIP**（未授权，不触发真实 LLM） |
| 3.2 数据源 / 网络耗时主导 | 需授权真实 AKShare/行情源受控对比 | **SKIP**（未授权） |
| 3.3 AKShare IPC 序列化 | 微基准显示序列化 7–9× 提升，但**未证明其在端到端耗时中占比显著**；5000 根响应序列化仅 ~9 ms，相对 AKShare HTTP 拉取数百毫秒可忽略；接近 8 MiB 上限时 ~81 ms 才可能可观 | **SKIP**（保留 stdlib；记录数据，待端到端基准后重新评估） |
| 3.4 本地 CPU 数值路径 | 需隔离任务内 profiler + 固定夹具金值回归 | **SKIP**（需授权运行真实任务） |

若后续获得授权，orjson 评估的必需验收清单（摘自计划 §3.3，不得跳过）：

- `akshare_worker.py` / `market/service.py` / `test_market_provider_process.py` 同步更新。
- 握手 `{"ready":true}\n`、行分隔、请求 ID、超时/重启、前台优先级不变。
- 父子进程 bytes/str 边界、整行大小上限不变。
- 中文、日期时间、空值、浮点与非有限数值（`allow_nan=False`）接受/拒绝行为与当前协议一致。
- 同一固定行情输入解析后的对象、后续领域输出与 `stable_hash`/`output_hash` 完全一致。
- 端到端中位耗时下降 ≥10% 且无错误率上升，才允许合并。

## 0.5 明确未做 / 待授权项

1. 未触发真实研究、真实供应商、真实 LLM、新回测、删除数据或生产迁移。
2. 未改动任何 `mem_limit`、CPU/PID/线程上限；未调整行情缓存或线程池。
3. 未引入 orjson，未更换模型、推理档位或 prompt。
4. `docs/API.md` 的 DUAL 拓扑 700 MiB 预算说明未改动（上限未变）。
5. 真实外部场景（网络延迟、限流、供应商流控）未验证，需授权后在单独报告区分。

## 0.6 复测建议（获得授权后）

在相同代码、配置哈希、固定输入、拓扑与缓存状态下，冷启动与热运行分开记录；本地固定输入
≥5 次取中位/最大/离散，真实供应商/模型场景 ≥3 次；阶段耗时直接从 audit `duration_ms` 读取。
复测后更新本表并给出“基线 vs 最终”对比。
