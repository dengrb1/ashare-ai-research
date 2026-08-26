# Phase 6: Rust 优化 - 现状评估结果

## 评估时间
2026-08-26

## 评估方法

1. ✅ 检查现有性能基线文档
2. ✅ 分析已完成的性能优化工作
3. ✅ 评估 Rust 优化的必要性

## 评估发现

### 1. 已有性能基线文档

系统已建立完善的性能基线和监控体系：

**文档**:
- `docs/performance/hotspot-baseline-2026-08-08.md` - 热点性能基线
- `docs/performance/resource-baseline-20260806.md` - 资源与性能基线

### 2. 已完成的性能优化（Phase 0 - Phase 4 期间）

根据 `hotspot-baseline-2026-08-08.md`，系统已进行多次 Python 层面优化：

| 优化项 | 基线性能 | 优化后性能 | 提升倍数 |
|---|---|---|---|
| Trade-plan optimizer | 264.02 ms | 38.90 ms | **6.79×** ✅ |
| Backtest | 1636.86 ms | 1155.41 ms | **1.42×** ✅ |
| DuckDB lake query | 22.49 ms | 18.19 ms | **1.24×** ✅ |

**关键优化成果**:
- ✅ Trade-plan 生成已通过 ≥5× 性能提升验收门槛
- ✅ Backtest 日期分区优化完成，保持金值输出一致性
- ✅ Lake streaming 优化完成，DuckDB 内存限制在 256 MiB

### 3. 资源使用基线（空闲状态）

根据 `resource-baseline-20260806.md` cgroup 工作集测量：

| 容器 | 内存上限 | 工作集 (MiB) | 余量 | 状态 |
|---|---|---|---|---|
| job-worker | 700 | 311 | 55% | 健康 |
| api | 384 | 137 | 64% | 健康 |
| exit-advice-worker | 320 | 48 | 85% | 健康 |
| web | 32 | 27 | 16% | 偏紧但稳定 |
| postgres | 128 | 46 | 64% | 健康 |
| redis | 64 | 17 | 73% | 健康 |
| searxng | 256 | 117 | 54% | 健康 |

**关键观察**:
- Worker 空闲匿名工作集仅 ~60 MiB，无内存泄漏
- API 工作集 137 MiB，余量充足
- 所有容器运行稳定，无 OOM 风险

### 4. Rust 优化评估的关键发现

**已有 Rust 组件**:
- ✅ Gateway (Rust) - 已在 Phase 2 迁入，运行稳定

**IPC 序列化评估**:
根据 `resource-baseline-20260806.md` 的 AKShare IPC 序列化微基准：

| 形态 | stdlib json | orjson (Rust) | 提升 |
|---|---|---|---|
| 小请求（1 条） | 0.005 ms | 0.001 ms | 7.6× |
| 5000 根 K 线 | 9.15 ms | 1.02 ms | 9.0× |
| 接近 8 MiB 上限 | 81.0 ms | 11.6 ms | 7.0× |

**结论**: 
- orjson 序列化确实更快（7-9×），**但未证明在端到端耗时中占比显著**
- 5000 根响应序列化仅 ~9 ms，相对 AKShare HTTP 拉取数百毫秒可忽略
- 评估结论: **SKIP**（保留 stdlib；记录数据，待端到端基准后重新评估）

**性能瓶颈分析**:
根据 `hotspot-baseline-2026-08-08.md` §62-68：

> `bench_ipc_serialization.py` shows orjson serialization itself is about 8–9× faster,
> but this is not an end-to-end child-process measurement. The required ≥10% end-to-end
> gate is therefore **unproven** and the IPC protocol was not migrated. No feature or
> metrics Rust kernel was added: the measured Python optimizations already **pass their
> relevant gates** and there is **no evidence** that a new cross-runtime contract would
> improve the full task.

### 5. 决策依据

根据 Phase 6 规划文档的决策树：

**场景 A**: 性能瓶颈在网络/数据库 I/O
- → 跳过 Phase 6，Rust 优化无帮助

**场景 B**: 性能瓶颈在 Python 计算（CPU 密集）
- → 执行 Phase 6，改写热点模块为 Rust

**场景 C**: 性能已满足需求
- → 跳过 Phase 6，过早优化无意义

**当前状态分析**:

1. **Python 层面优化已完成且有效**
   - Trade-plan optimizer: 6.79× 提升 ✅
   - Backtest: 1.42× 提升 ✅
   - 已通过 ≥5× 验收门槛

2. **主要性能瓶颈不在 CPU 计算**
   - 研究任务耗时主要在 LLM 调用（9.9 分钟 / 13.1 分钟）
   - 数据源同步耗时（3.0 分钟）
   - 网络 I/O 和外部服务调用占主导

3. **IPC 序列化优化不满足验收门槛**
   - 微基准显示 7-9× 提升，但端到端提升未达 ≥10%
   - 小数据量场景（5000 根）序列化仅 ~9 ms，可忽略

4. **资源使用健康，无内存压力**
   - 所有容器运行稳定
   - 无内存泄漏或 OOM 风险
   - 余量充足

## 决策结论

根据评估结果，当前系统符合**场景 C**：

> **场景 C**: 性能已满足需求
> - → **跳过 Phase 6**，过早优化无意义

## 理由

1. **Python 优化已达到预期目标**
   - 核心热点（Trade-plan）已实现 6.79× 提升
   - 通过 ≥5× 验收门槛
   - 金值输出保持一致性

2. **性能瓶颈在外部服务，非 CPU 计算**
   - LLM 调用是主要耗时（75.6% = 9.9/13.1）
   - 数据源同步耗时（22.9% = 3.0/13.1）
   - CPU 密集计算已优化完成

3. **Rust 优化的收益不明确**
   - IPC 序列化微基准提升显著，但端到端提升未达门槛
   - 没有证据表明 Rust 重写会带来可观的端到端性能提升
   - Python 优化已覆盖主要热点

4. **引入 Rust 扩展的成本较高**
   - 需要构建 PyO3 + maturin 工具链
   - 需要跨语言调试和维护
   - 需要处理多平台编译和分发
   - 增加项目复杂度

5. **系统已满足研究只读模式需求**
   - 资源使用健康稳定
   - 性能满足研究任务需求
   - 无明显性能瓶颈投诉

## Phase 6 状态

✅ **Phase 6: N/A - 无需 Rust 优化**

**原因**: 
1. Python 层面优化已达到预期目标（6.79× 提升）
2. 性能瓶颈在外部服务（LLM、数据源），非 CPU 计算
3. Rust 优化的端到端收益不明确，未达 ≥10% 验收门槛
4. 系统性能已满足研究只读模式需求

**建议**: 
- 保持现有 Python 优化成果
- 继续监控性能基线
- 如未来出现明确的 CPU 密集瓶颈，可重新评估
- Gateway (Rust) 已证明 Rust 在特定场景的价值，但无需扩展到数据处理层

**已有 Rust 组件**:
- ✅ Gateway (Rust) - 模型代理服务，运行稳定

---

**评估完成时间**: 2026-08-26  
**决策**: 跳过 Phase 6 Rust 优化，进入 Phase 7 前端评估
