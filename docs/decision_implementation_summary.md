# A-share AI Trader 决策模型改造实施总结

## 完成状态

已完成统一决策协议和模式路由的核心架构，Legacy/Jev 双模式框架就绪。

## 已实施的模块

### 1. 决策领域核心 (`src/ashare_ai/agents/decision/`)

- ✅ **models.py**: 统一决策协议模型
  - `UnifiedDecision`: 统一决策输出，包含动作、风险、仓位和全部概率
  - `MarketState`: 市场状态输入，OHLCV + 技术指标 + 基本面 + 情绪
  - `DecisionProbabilities`: 6 个任务头的概率分布（方向 1d/5d、涨幅、动作、风险）
  - 严格校验：概率和为 1、时间戳、OHLC 合法性

- ✅ **protocols.py**: `DecisionProvider` 接口
  - 所有决策模式必须实现的统一接口
  - `predict(MarketState) -> UnifiedDecision`

- ✅ **legacy.py**: Legacy Provider
  - 将现有 builtin/LLM 组件结果转换为统一决策
  - 骨架实现，基于确定性评分映射
  - 保留旧 Provider 接口兼容

- ✅ **jev.py**: Jev Provider（骨架）
  - 轻量多任务 Transformer 架构设计
  - 延迟加载模型，支持 CPU/CUDA
  - PyTorch 作为可选依赖
  - 当前为骨架，需补充完整训练和推理

- ✅ **router.py**: 决策路由器
  - 根据配置选择 Legacy/Jev
  - Fail-closed 默认：异常时停止并报错
  - 可选 fallback：显式开启后才回退 Legacy
  - 统一审计日志

- ✅ **market_state.py**: Market State 构建器
  - 从 CanonicalDailyBundle 提取特征
  - 处理缺失字段（中性值 + mask）
  - 禁止未来数据泄漏

- ✅ **backtest_adapter.py**: 回测适配器
  - `decision_to_signal()`: 转换为 BacktestSignal
  - `enrich_signal_with_decision_metadata()`: 扩展决策元数据
  - Position 枚举映射为 target_weight

### 2. 配置扩展 (`src/ashare_ai/core/config.py`)

- ✅ `decision_mode: Literal["legacy", "jev"]` (默认 legacy)
- ✅ `decision_fallback_enabled: bool` (默认 false)
- ✅ `jev_model_dir`, `jev_model_version`, `jev_device`, `jev_checkpoint`

### 3. 回测扩展 (`src/ashare_ai/backtest/engine.py`)

- ✅ `BacktestConfig.decision_mode` 字段（可选，向后兼容）

### 4. 训练基础设施 (`src/ashare_ai/decision_training/`)

- ✅ 骨架实现：`JevTrainer`, `DatasetManifest`
- 🔲 TODO: 完整训练流程（数据集生成、时序切分、模型定义、训练循环）

### 5. 测试 (`tests/unit/agents/decision/`)

- ✅ **test_models.py**: 概率和校验、时间校验、OHLC 校验（16 个测试，全部通过）
- ✅ **test_legacy.py**: Legacy Provider 输出统一协议（3 个测试）
- ✅ **test_jev.py**: Jev Provider 加载和错误处理（4 个测试）
- ✅ **test_router.py**: 模式切换、fallback 逻辑（5 个测试）
- ✅ **test_backtest_adapter.py**: 决策到信号转换（4 个测试）
- ✅ **总计 32 个测试全部通过**

### 6. 文档

- ✅ **docs/decision_mode.md**: 配置说明、使用示例、安全边界

## 未实施的部分

### 高优先级（核心功能）

1. **CLI 扩展** (`src/ashare_ai/cli.py`)
   - `ashare-ai predict --symbol 600000.SH --mode legacy|jev`
   - `ashare-ai train-jev --dataset ... --output ...`
   - `ashare-ai backtest --mode legacy|jev`
   - `ashare-ai backtest --compare`

2. **API 扩展** (`src/ashare_ai/api/`)
   - `GET /api/v1/decision/mode`
   - `POST /api/v1/decision/predict`
   - `POST /api/v1/decision/train`
   - `GET /api/v1/decision/models`
   - `POST /api/v1/backtests/compare`

3. **GUI 扩展** (`web/src/`)
   - 系统设置页：decision_mode 切换
   - 研究/回测页：显示当前模式
   - 模型管理页：Jev 版本和 checkpoint 选择

4. **Jev 模型完整实现**
   - 数据集生成（从冻结 Bundle + 特征快照）
   - 标签生成（未来窗口，不泄漏输入）
   - 时序切分（train/val/test walk-forward）
   - Transformer 架构（共享编码器 + 6 个 head）
   - 训练循环（损失、优化器、学习率调度、早停）
   - Checkpoint 保存和版本管理

5. **Legacy Provider 真实集成**
   - 连接现有 `orchestration.builtin`
   - 调用 `scoring.formula.build_composite_score()`
   - 从 `CompositeScore` 转换为 `UnifiedDecision`

### 中优先级（增强功能）

6. **系统设置中心集成** (`src/ashare_ai/core/system_settings.py`)
   - 允许管理员修改 decision_mode 和 fallback_enabled
   - 允许切换 Jev 模型版本和 checkpoint

7. **回测引擎集成** (`src/ashare_ai/orchestration/builtin_backtest.py`)
   - 在回测流程中调用 DecisionRouter
   - 记录 mode/model_version/概率到回测结果
   - 对比回测：并行运行 Legacy 和 Jev

8. **审计扩展** (`src/ashare_ai/observability/audit.py`)
   - 记录每次决策的 mode/model_version/probabilities
   - 记录 fallback 事件和原因
   - 记录 input_manifest_hash

### 低优先级（完善功能）

9. **集成测试**
   - 端到端决策流程测试
   - 回测对比测试
   - CLI/API 集成测试

10. **文档补充**
    - 训练数据集格式说明
    - Jev 模型架构文档
    - 性能基准和对比报告

## 架构亮点

1. **Fail-closed 默认**：异常时停止，不静默回退，确保可观测性
2. **向后兼容**：Legacy 模式保持现有行为，旧配置自动使用默认值
3. **共享基础设施**：复用 Bundle、风险控制、回测引擎、审计
4. **严格安全边界**：保持 RESEARCH_ONLY 模式，不新增真实交易路径
5. **完整审计**：记录模式、版本、概率、fallback 事件

## 下一步建议

### 第一阶段：验证架构
1. 实现 Legacy Provider 真实集成，验证统一协议可行性
2. 添加简单的 CLI 命令 `ashare-ai predict --mode legacy`
3. 手动测试决策流程

### 第二阶段：Jev 原型
1. 实现数据集生成和标签生成
2. 训练一个简单的 Jev 基线模型
3. 验证推理流程和性能

### 第三阶段：回测集成
1. 在回测引擎中调用 DecisionRouter
2. 实现对比回测
3. 生成性能报告

### 第四阶段：API 和 GUI
1. 扩展 API 端点
2. 更新系统设置页
3. 添加模型管理界面

## 测试验证

```powershell
# 运行决策模块测试
$env:PYTHONPATH='src'
python -m pytest tests/unit/agents/decision/ -v

# 结果：32 passed, 1 warning
```

所有核心组件测试通过，架构验证成功。
