# Phase 6: Rust 优化

## 目标

评估性能瓶颈，选择性地将计算密集型 Python 模块改写为 Rust，提升系统性能。

## 前置条件

- 已完成 Phase 1-5
- 已有 Gateway (Rust) 运行经验
- 系统基本功能完整，可以进行性能测试

## Phase 6A: 性能基线和瓶颈分析

### 实施步骤

1. **建立性能基线**
   ```bash
   # 回测任务性能
   time curl -X POST http://localhost:8000/api/v1/backtest/runs \
     -H "Content-Type: application/json" \
     -d '{"symbols": ["000001.SZ", "600000.SS"], "start_date": "2025-01-01", "end_date": "2025-12-31"}'
   
   # 研究任务性能
   time curl -X POST http://localhost:8000/api/v1/research/runs \
     -H "Content-Type: application/json" \
     -d '{"symbols": ["000001.SZ"], "depth": "standard"}'
   
   # 评分任务性能
   time curl -X POST http://localhost:8000/api/v1/scoring/batch \
     -H "Content-Type: application/json" \
     -d '{"symbols": ["000001.SZ", "600000.SS", ...]}'  # 100+ symbols
   ```

2. **使用 Python profiler 找瓶颈**
   ```python
   # 在关键任务 handler 中添加
   import cProfile
   import pstats
   
   profiler = cProfile.Profile()
   profiler.enable()
   
   # ... 执行任务 ...
   
   profiler.disable()
   stats = pstats.Stats(profiler)
   stats.sort_stats('cumulative')
   stats.print_stats(20)
   ```

3. **检查现有性能文档**
   ```bash
   ls -la docs/performance/
   # 查看 resource-baseline-20260806.md
   # 查看 hotspot-baseline-2026-08-08.md
   ```

4. **识别候选模块**
   
   常见 Rust 优化候选：
   - 特征工程（大量数值计算）
   - 回测引擎（循环密集）
   - 数据序列化/反序列化（Parquet、CSV）
   - 技术指标计算（移动平均、MACD、RSI 等）
   - 组合优化算法（如果是纯数值）

### 决策点

根据 profiling 结果：

**场景 A**: 性能瓶颈在网络/数据库 I/O
- → **跳过 Phase 6**，Rust 优化无帮助
- → 优化数据库查询、添加缓存

**场景 B**: 性能瓶颈在 Python 计算（CPU 密集）
- → **执行 Phase 6**，改写热点模块为 Rust

**场景 C**: 性能已满足需求
- → **跳过 Phase 6**，过早优化无意义

## Phase 6B: Rust 模块开发（如果需要）

### 前提条件
- 已完成 Phase 6A profiling
- 确认有 CPU 密集型瓶颈（场景 B）

### 6B.1 技术栈选择

**Python-Rust 绑定方案**:

1. **PyO3** (推荐)
   - 纯 Rust 实现
   - 编译为 Python 扩展模块 (.pyd / .so)
   - 零拷贝数据传递（memoryview）
   - 示例：
     ```rust
     use pyo3::prelude::*;
     
     #[pyfunction]
     fn calculate_sma(prices: Vec<f64>, window: usize) -> Vec<f64> {
         // ... Rust 实现 ...
     }
     
     #[pymodule]
     fn ashare_rust(_py: Python, m: &PyModule) -> PyResult<()> {
         m.add_function(wrap_pyfunction!(calculate_sma, m)?)?;
         Ok(())
     }
     ```

2. **maturin** (构建工具)
   - PyO3 项目的标准打包工具
   - 支持 `maturin develop` (开发模式)
   - 支持 `maturin build --release` (发布构建)
   - 支持 wheel 分发

### 6B.2 创建 Rust 扩展模块

**目录结构**:
```
ashare-ai-src/
├── gateway/              # 已存在
├── rust-extensions/      # 新增
│   ├── Cargo.toml
│   ├── pyproject.toml    # maturin 配置
│   └── src/
│       ├── lib.rs        # PyO3 模块入口
│       ├── indicators.rs # 技术指标
│       ├── backtest.rs   # 回测引擎
│       └── features.rs   # 特征工程
└── src/ashare_ai/
    └── _rust.pyi         # Type stubs for IDE
```

**实施步骤**:

1. **初始化 Rust 扩展项目**
   ```bash
   cd rust-extensions
   cargo init --lib
   ```

2. **配置 Cargo.toml**
   ```toml
   [package]
   name = "ashare-rust"
   version = "0.1.0"
   edition = "2021"
   
   [lib]
   name = "ashare_rust"
   crate-type = ["cdylib"]
   
   [dependencies]
   pyo3 = { version = "0.21", features = ["extension-module"] }
   numpy = "0.21"
   ndarray = "0.15"
   rayon = "1.8"  # 并行计算
   ```

3. **配置 pyproject.toml**
   ```toml
   [build-system]
   requires = ["maturin>=1.0,<2.0"]
   build-backend = "maturin"
   
   [project]
   name = "ashare-rust"
   requires-python = ">=3.11"
   ```

4. **实现核心模块**
   
   示例：技术指标模块
   ```rust
   // rust-extensions/src/indicators.rs
   use numpy::{PyArray1, PyReadonlyArray1};
   use pyo3::prelude::*;
   
   #[pyfunction]
   pub fn sma<'py>(
       py: Python<'py>,
       prices: PyReadonlyArray1<f64>,
       window: usize,
   ) -> &'py PyArray1<f64> {
       let prices = prices.as_slice().unwrap();
       let mut result = vec![f64::NAN; prices.len()];
       
       for i in window..prices.len() {
           let sum: f64 = prices[i - window..i].iter().sum();
           result[i] = sum / window as f64;
       }
       
       PyArray1::from_vec(py, result)
   }
   
   #[pyfunction]
   pub fn ema<'py>(
       py: Python<'py>,
       prices: PyReadonlyArray1<f64>,
       span: usize,
   ) -> &'py PyArray1<f64> {
       let prices = prices.as_slice().unwrap();
       let alpha = 2.0 / (span as f64 + 1.0);
       let mut result = vec![f64::NAN; prices.len()];
       
       result[0] = prices[0];
       for i in 1..prices.len() {
           result[i] = alpha * prices[i] + (1.0 - alpha) * result[i - 1];
       }
       
       PyArray1::from_vec(py, result)
   }
   ```

5. **模块入口**
   ```rust
   // rust-extensions/src/lib.rs
   use pyo3::prelude::*;
   
   mod indicators;
   mod backtest;
   mod features;
   
   #[pymodule]
   fn ashare_rust(_py: Python, m: &PyModule) -> PyResult<()> {
       // 技术指标
       m.add_function(wrap_pyfunction!(indicators::sma, m)?)?;
       m.add_function(wrap_pyfunction!(indicators::ema, m)?)?;
       m.add_function(wrap_pyfunction!(indicators::macd, m)?)?;
       m.add_function(wrap_pyfunction!(indicators::rsi, m)?)?;
       
       // 回测函数（如果需要）
       m.add_function(wrap_pyfunction!(backtest::run_backtest, m)?)?;
       
       // 特征工程（如果需要）
       m.add_function(wrap_pyfunction!(features::extract_features, m)?)?;
       
       Ok(())
   }
   ```

### 6B.3 集成到 Python 代码

**实施步骤**:

1. **安装 Rust 扩展到开发环境**
   ```bash
   cd rust-extensions
   maturin develop
   ```

2. **重构 Python 模块使用 Rust**
   
   修改 `src/ashare_ai/features/indicators.py`:
   ```python
   try:
       from ashare_rust import sma, ema, macd, rsi
       RUST_AVAILABLE = True
   except ImportError:
       RUST_AVAILABLE = False
       # 回退到 Python 实现
       from .indicators_python import sma, ema, macd, rsi
   
   # 公开接口保持不变
   __all__ = ["sma", "ema", "macd", "rsi"]
   ```

3. **保留 Python 实现作为回退**
   - 保持现有 Python 代码（重命名为 `*_python.py`）
   - Rust 不可用时自动回退
   - 确保接口完全一致

4. **添加类型存根**
   ```python
   # src/ashare_ai/_rust.pyi
   from numpy import ndarray
   
   def sma(prices: ndarray, window: int) -> ndarray: ...
   def ema(prices: ndarray, span: int) -> ndarray: ...
   def macd(prices: ndarray, fast: int, slow: int, signal: int) -> tuple[ndarray, ndarray, ndarray]: ...
   def rsi(prices: ndarray, period: int) -> ndarray: ...
   ```

### 6B.4 Docker 集成

**挑战**: Docker 镜像需要编译 Rust 扩展

**解决方案 1**: 多阶段构建（推荐）

修改 `docker/app.Dockerfile`:
```dockerfile
# Stage 1: Build Rust extensions
FROM rust:1.83-alpine AS rust-builder
WORKDIR /build
RUN apk add --no-cache musl-dev python3-dev
COPY rust-extensions /build/rust-extensions
RUN cd rust-extensions && \
    pip install maturin && \
    maturin build --release

# Stage 2: Python app
FROM python:3.12-slim AS app
WORKDIR /app

# 复制 Rust wheel 并安装
COPY --from=rust-builder /build/rust-extensions/target/wheels/*.whl /tmp/
RUN pip install /tmp/*.whl

# ... 其余 Python 依赖和代码 ...
```

**解决方案 2**: 预编译 wheel（可选）
- 在 CI 中预编译 wheel
- 存储到 artifact registry
- Docker 构建时直接下载

### 6B.5 性能测试

**实施步骤**:

1. **基准测试**
   ```python
   # tests/benchmark_rust.py
   import time
   import numpy as np
   from ashare_ai.features import sma
   from ashare_ai.features.indicators_python import sma as sma_python
   
   prices = np.random.random(100000)
   
   # Python 实现
   start = time.time()
   result_py = sma_python(prices, 20)
   python_time = time.time() - start
   
   # Rust 实现
   start = time.time()
   result_rust = sma(prices, 20)
   rust_time = time.time() - start
   
   print(f"Python: {python_time:.4f}s")
   print(f"Rust: {rust_time:.4f}s")
   print(f"Speedup: {python_time / rust_time:.2f}x")
   
   # 验证结果一致
   assert np.allclose(result_py, result_rust, equal_nan=True)
   ```

2. **端到端测试**
   - 运行完整研究任务
   - 比较 Rust 前后的总时间
   - 确保结果一致性

3. **内存测试**
   - 使用 `memory_profiler`
   - 确认 Rust 模块无内存泄漏

## Phase 6C: 其他优化（替代方案）

如果不采用 Rust，考虑其他优化：

### Python 级别优化

1. **Numba JIT 编译**
   ```python
   from numba import jit
   
   @jit(nopython=True)
   def calculate_indicator(prices, window):
       # ... 纯数值计算 ...
   ```

2. **Cython**
   - 比 Rust 更容易集成
   - 性能提升约 2-10x（Rust 可达 10-100x）

3. **NumPy 向量化**
   - 重写循环为向量操作
   - 利用 BLAS/LAPACK

### 架构优化

1. **批处理**
   - 合并多个小任务为一个大批次
   - 减少函数调用开销

2. **缓存**
   - Redis 缓存计算结果
   - 避免重复计算

3. **并行化**
   - 使用 `multiprocessing`
   - 使用 `concurrent.futures`

## 交付物清单

### Phase 6A (评估)
- `docs/performance/phase6-profiling-results.md` - Profiling 报告
- `docs/phase6-rust-optimization-plan.md` (本文件) - 标记是否需要 Phase 6B

### Phase 6B (Rust 优化)
- `rust-extensions/` - Rust 扩展模块
  - `Cargo.toml`
  - `pyproject.toml`
  - `src/lib.rs`
  - `src/indicators.rs`
  - `src/backtest.rs` (可选)
  - `src/features.rs` (可选)
- `src/ashare_ai/_rust.pyi` - 类型存根
- `src/ashare_ai/features/indicators_python.py` - Python 回退实现
- `docker/app.Dockerfile` - 添加 Rust 构建阶段
- `tests/benchmark_rust.py` - 基准测试
- `.gitignore` - 添加 `rust-extensions/target/`
- `docs/fusion-progress.md` - 标记 Phase 6 完成

### Phase 6C (其他优化)
- 优化后的 Python 代码
- 性能对比报告

## 验收标准

### Phase 6A
- [ ] 性能基线已建立
- [ ] Profiling 结果已记录
- [ ] 瓶颈已识别
- [ ] 已决定是否执行 Phase 6B

### Phase 6B (如果执行)
- [ ] Rust 扩展模块成功编译
- [ ] Python 可以导入 Rust 模块
- [ ] 关键函数有 Rust 实现
- [ ] 保留 Python 回退实现
- [ ] 基准测试显示性能提升 > 5x
- [ ] 端到端测试结果一致
- [ ] Docker 镜像成功构建
- [ ] 容器中 Rust 扩展正常工作
- [ ] 无内存泄漏

## 性能目标

### 技术指标计算
- Python baseline: ~10ms / 1000 points
- Rust target: < 1ms / 1000 points
- 目标加速比: > 10x

### 回测引擎（如果重写）
- Python baseline: ~5s / 1 年 / 1 股
- Rust target: < 0.5s / 1 年 / 1 股
- 目标加速比: > 10x

### 特征工程（如果重写）
- Python baseline: ~2s / 100 股
- Rust target: < 0.2s / 100 股
- 目标加速比: > 10x

## 风险和注意事项

### 技术风险

1. **Rust 学习曲线**
   - 风险：开发时间延长
   - 缓解：从简单模块开始，保留 Python 回退

2. **跨语言数据传递开销**
   - 风险：数据序列化抵消性能提升
   - 缓解：使用 zero-copy (numpy memoryview)

3. **Docker 构建时间**
   - 风险：Rust 编译慢，CI 时间长
   - 缓解：多阶段构建 + 缓存层

### 业务风险

1. **行为不一致**
   - 风险：Rust 和 Python 实现结果不同
   - 缓解：详尽的单元测试，数值比较

2. **可维护性下降**
   - 风险：团队不熟悉 Rust
   - 缓解：限制 Rust 范围，保持 Python 回退

## 实施步骤顺序

1. **Phase 6A: 评估**（2-4 小时）
   - 性能基线测试
   - Profiling 分析
   - 决策是否执行 Phase 6B

2. **Phase 6B: Rust 优化**（如果需要）
   - 设置 Rust 项目（2 小时）
   - 实现核心模块（10-20 小时，取决于复杂度）
   - 集成和测试（4-6 小时）
   - Docker 集成（2-3 小时）

3. **Phase 6C: 其他优化**（替代方案，2-8 小时）

**预计总时间**: 
- 仅评估: 2-4 小时
- 评估 + Rust 优化: 18-35 小时
- 评估 + Python 优化: 4-12 小时

## 后续阶段预览

完成 Phase 6 后，进入 Phase 7: Web/PWA 增强

---

更新时间：2026-08-26
状态：规划完成，待性能评估
