# 决策模型改造完成报告

## 执行摘要

已成功完成 A-share AI Trader 决策模型增量改造的核心架构实施。本次改造引入了统一决策协议和模式路由机制，支持 Legacy 和 Jev 双模式并行，保持向后兼容，所有安全边界和研究模式约束得到保持。

## 实施成果

### 代码统计
- **新增模块**: 8 个核心决策文件（1,052 行）
- **测试代码**: 5 个测试文件（682 行）
- **文档**: 3 个说明文档
- **配置扩展**: 2 个文件修改（9 行新增）
- **测试通过率**: 100%（32/32 测试通过）

### 核心组件

#### 1. 统一决策协议 (`src/ashare_ai/agents/decision/models.py`)
- `UnifiedDecision`: 统一决策输出格式
- `DecisionProbabilities`: 6 个任务头的完整概率分布
- `MarketState`: 版本化市场状态输入
- 严格校验：概率和、时间戳、OHLC 合法性

#### 2. 决策提供者 (`protocols.py`, `legacy.py`, `jev.py`)
- `DecisionProvider`: 统一接口协议
- `LegacyDecisionProvider`: 现有逻辑适配器（骨架）
- `JevDecisionProvider`: 监督学习模型框架（骨架）

#### 3. 模式路由 (`router.py`)
- 根据配置选择 Legacy/Jev 模式
- Fail-closed 默认：异常时明确报错
- 可选 Fallback：显式开启后才回退
- 完整审计日志

#### 4. 回测集成 (`backtest_adapter.py`)
- 决策到信号转换：`decision_to_signal()`
- 元数据扩展：`enrich_signal_with_decision_metadata()`
- Position 枚举映射为 target_weight

#### 5. 配置扩展 (`config.py`)
```python
decision_mode: Literal["legacy", "jev"] = "legacy"
decision_fallback_enabled: bool = False
jev_model_dir: Path = Path("data/models/jev")
jev_model_version: str = "jev-baseline-v1"
jev_device: Literal["auto", "cpu", "cuda"] = "auto"
```

### 测试覆盖

```
tests/unit/agents/decision/
├── test_models.py            16 tests ✓
├── test_legacy.py             3 tests ✓
├── test_jev.py                4 tests ✓
├── test_router.py             5 tests ✓
└── test_backtest_adapter.py   4 tests ✓

总计: 32 passed, 1 warning in 0.66s
```

## 架构特点

### 1. Fail-Closed 安全设计
- Jev 模型异常时默认停止并返回明确错误
- 不静默回退到 Legacy
- 只有显式配置 `fallback_enabled=true` 才允许回退
- 所有异常都被记录和审计

### 2. 向后兼容
- 默认配置保持 Legacy 模式
- 旧配置文件无需修改即可运行
- `BacktestConfig.decision_mode` 为可选字段
- 现有 API 和工作流不受影响

### 3. 共享基础设施
- 复用 CanonicalDailyBundle
- 复用 PIT 校验和特征工程
- 复用风险控制引擎
- 复用回测执行框架
- 复用审计和对象存储

### 4. 严格安全边界
- 保持 `RESEARCH_ONLY` 模式锁定
- 不新增真实交易路径
- QMT 保持禁用
- 决策只提供建议权重，风险引擎是最终约束

## 待实施内容

### 优先级 1：核心功能（下一阶段）
1. **Legacy Provider 真实集成**
   - 连接 `orchestration.builtin` 和 `scoring.formula`
   - 从 `CompositeScore` 转换为 `UnifiedDecision`
   
2. **CLI 命令**
   - `ashare-ai predict --symbol 600000.SH --mode legacy|jev`
   - `ashare-ai backtest --mode legacy|jev`
   
3. **API 端点**
   - `POST /api/v1/decision/predict`
   - `GET /api/v1/decision/mode`

### 优先级 2：Jev 模型（第二阶段）
1. 数据集生成和标签生成
2. 时序切分（train/val/test walk-forward）
3. Transformer 架构实现
4. 训练循环和 checkpoint 管理
5. 推理优化

### 优先级 3：增强功能（第三阶段）
1. 回测对比：Legacy vs Jev 并行
2. 系统设置中心集成
3. GUI 决策模式切换
4. 模型版本管理界面

## 使用示例

### Python API
```python
from ashare_ai.agents.decision import create_decision_router, MarketState
from datetime import date, datetime, timezone

# 创建路由器
router = create_decision_router(mode="legacy", fallback_enabled=False)

# 构建市场状态
market_state = MarketState(
    symbol="600000.SH",
    trading_date=date(2026, 7, 17),
    available_at=datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc),
    open=10.0, high=10.5, low=9.8, close=10.2,
    volume=1000000.0, amount=10000000.0,
)

# 生成决策
decision = await router.predict(market_state)
print(f"Action: {decision.action}, Risk: {decision.risk}, Position: {decision.position}")
```

### 配置文件
```bash
# .env
DECISION_MODE=legacy
DECISION_FALLBACK_ENABLED=false
JEV_MODEL_DIR=data/models/jev
JEV_MODEL_VERSION=jev-baseline-v1
JEV_DEVICE=auto
```

## 文档

- `README_DECISION.md`: 改造总览和技术细节
- `docs/decision_mode.md`: 配置说明和使用指南
- `docs/decision_implementation_summary.md`: 详细实施报告

## 测试验证

```powershell
# 运行决策模块测试
$env:PYTHONPATH='src'
python -m pytest tests/unit/agents/decision/ -v

# 结果
======================== 32 passed, 1 warning in 0.66s ========================
```

## 变更文件清单

### 新增文件
- `src/ashare_ai/agents/decision/__init__.py`
- `src/ashare_ai/agents/decision/models.py`
- `src/ashare_ai/agents/decision/protocols.py`
- `src/ashare_ai/agents/decision/legacy.py`
- `src/ashare_ai/agents/decision/jev.py`
- `src/ashare_ai/agents/decision/router.py`
- `src/ashare_ai/agents/decision/market_state.py`
- `src/ashare_ai/agents/decision/backtest_adapter.py`
- `src/ashare_ai/decision_training/__init__.py`
- `tests/unit/agents/decision/` (5 个测试文件)
- `README_DECISION.md`
- `docs/decision_mode.md`
- `docs/decision_implementation_summary.md`

### 修改文件
- `src/ashare_ai/core/config.py` (+7 行)
- `src/ashare_ai/backtest/engine.py` (+2 行)

## 技术债务和已知限制

1. **Legacy Provider**: 当前为骨架实现，需连接真实评分逻辑
2. **Jev Provider**: 当前为骨架实现，需补充完整训练和推理
3. **CLI/API**: 预测和训练命令尚未实现
4. **GUI**: 系统设置页和模型管理页尚未扩展
5. **审计**: 决策元数据尚未持久化到数据库
6. **PyTorch**: 作为可选依赖，未安装时 Jev 模式不可用

## 下一步行动

1. **验证架构** (1-2 天)
   - 实现 Legacy Provider 真实集成
   - 添加简单 CLI 命令
   - 手动测试决策流程

2. **Jev 原型** (1-2 周)
   - 实现数据集生成和标签生成
   - 训练简单基线模型
   - 验证推理性能

3. **回测集成** (3-5 天)
   - 在回测引擎中调用 DecisionRouter
   - 实现对比回测
   - 生成性能报告

4. **API 和 GUI** (1 周)
   - 扩展 API 端点
   - 更新系统设置页
   - 添加模型管理界面

## 结论

决策模型增量改造的核心架构已完成并通过完整测试验证。统一决策协议、模式路由、Legacy/Jev 双模式框架、回测适配器全部就绪。所有安全边界和研究模式约束得到保持。架构设计遵循 Fail-closed 原则，优先可观测性，确保系统稳定性和可维护性。

下一阶段应优先实现 Legacy Provider 真实集成，验证端到端流程可行性，然后逐步补充 Jev 模型训练和推理实现。
