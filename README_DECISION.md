"""
A-share AI Trader 决策模型增量改造 - 已完成核心架构

## 改造内容摘要

本次改造在现有 a-share-ai-trader 上引入了统一决策协议和模式路由，保留 Legacy 路径作为默认行为，
并提供可选的 Jev-like 监督学习模型框架。所有模式共享原有 Market Bundle、PIT 校验、风险控制、
回测执行、审计和模拟组合链路。

## 核心成果

### 1. 统一决策协议
- **UnifiedDecision**: 包含动作（BUY/HOLD/SELL）、风险（LOW/MEDIUM/HIGH）、仓位（0-100）和全部概率分布
- **DecisionProbabilities**: 6 个任务头输出（1日方向、5日方向、5日>3%涨幅、动作、风险、仓位）
- **MarketState**: 版本化市场状态输入，包含 OHLCV、技术指标、基本面、情绪特征
- **严格校验**: 概率和为 1、时间戳一致性、OHLC 合法性、PIT 约束

### 2. 决策模式路由
- **DecisionRouter**: 根据配置选择 Legacy 或 Jev Provider
- **Fail-closed 默认**: 异常时停止并明确报错，不静默回退
- **可选 Fallback**: 显式开启后才允许 Jev -> Legacy 回退
- **审计完整**: 记录 mode、model_version、概率、fallback 事件

### 3. Legacy Provider 适配器
- 将现有评分和组件结果转换为统一决策协议
- 骨架实现，基于确定性映射规则
- 保留旧接口兼容性

### 4. Jev Provider 框架
- 轻量多任务 Transformer 架构设计
- PyTorch 作为可选依赖，支持 CPU/CUDA
- 延迟加载模型
- 当前为骨架，需补充完整训练和推理实现

### 5. 回测集成适配器
- `decision_to_signal()`: 将 UnifiedDecision 转换为 BacktestSignal
- Position 枚举映射为 target_weight
- 决策元数据扩展：保留完整概率分布和模式信息

### 6. 配置扩展
```python
decision_mode: Literal["legacy", "jev"] = "legacy"
decision_fallback_enabled: bool = False
jev_model_dir: Path = Path("data/models/jev")
jev_model_version: str = "jev-baseline-v1"
jev_device: Literal["auto", "cpu", "cuda"] = "auto"
jev_checkpoint: Path | None = None
```

### 7. 完整测试覆盖
- 32 个单元测试全部通过
- 覆盖：概率校验、时间校验、模式切换、fallback 逻辑、回测适配

## 技术实现细节

### 架构设计原则
1. **向后兼容**: 默认配置保持 Legacy 模式，旧配置无缝工作
2. **Fail-closed**: 优先可观测性而非可用性，异常明确报错
3. **共享基础设施**: 复用所有现有风险控制和数据链路
4. **严格安全边界**: 保持 RESEARCH_ONLY，不新增真实交易路径

### 文件结构
```
src/ashare_ai/
├── agents/decision/          # 决策领域核心
│   ├── __init__.py
│   ├── models.py             # 统一协议模型
│   ├── protocols.py          # DecisionProvider 接口
│   ├── legacy.py             # Legacy Provider
│   ├── jev.py                # Jev Provider（骨架）
│   ├── router.py             # 模式路由器
│   ├── market_state.py       # Market State 构建器
│   └── backtest_adapter.py   # 回测适配器
├── core/config.py            # 新增决策配置字段
├── backtest/engine.py        # BacktestConfig 新增 decision_mode
└── decision_training/        # 训练基础设施（骨架）
    └── __init__.py

tests/unit/agents/decision/   # 完整测试覆盖
├── test_models.py            # 16 个测试
├── test_legacy.py            # 3 个测试
├── test_jev.py               # 4 个测试
├── test_router.py            # 5 个测试
└── test_backtest_adapter.py  # 4 个测试

docs/
├── decision_mode.md                    # 配置和使用说明
└── decision_implementation_summary.md  # 实施总结
```

## 未实施部分（后续阶段）

### 阶段 1: 验证和集成（优先级：高）
- [ ] Legacy Provider 真实集成（连接 orchestration.builtin 和 scoring.formula）
- [ ] CLI 命令：`ashare-ai predict --mode legacy|jev`
- [ ] API 端点：`POST /api/v1/decision/predict`
- [ ] 系统设置中心：允许切换 decision_mode

### 阶段 2: Jev 模型实现（优先级：高）
- [ ] 数据集生成（从冻结 Bundle + 特征快照）
- [ ] 标签生成（未来窗口，不泄漏输入）
- [ ] 时序切分（train/val/test walk-forward）
- [ ] Transformer 架构实现（共享编码器 + 6 个 head）
- [ ] 训练循环（损失、优化器、学习率调度、早停）
- [ ] Checkpoint 版本管理

### 阶段 3: 回测和对比（优先级：中）
- [ ] 回测引擎调用 DecisionRouter
- [ ] 对比回测：Legacy vs Jev 并行
- [ ] 性能指标：胜率、盈亏比、换手率、夏普比率
- [ ] CLI: `ashare-ai backtest --compare`

### 阶段 4: UI 和工具（优先级：中）
- [ ] GUI 系统设置页：decision_mode 切换
- [ ] GUI 模型管理页：Jev 版本和 checkpoint 选择
- [ ] GUI 研究页：显示当前决策模式
- [ ] CLI: `ashare-ai train-jev --dataset ... --output ...`

### 阶段 5: 完善和优化（优先级：低）
- [ ] 集成测试：端到端决策流程
- [ ] 性能优化：Jev 推理加速
- [ ] 文档补充：训练数据集格式、模型架构
- [ ] 审计扩展：记录决策元数据到数据库

## 测试验证

```powershell
# 运行决策模块测试
$env:PYTHONPATH='src'
python -m pytest tests/unit/agents/decision/ -v

# 结果：32 passed, 1 warning in 0.34s
```

所有核心协议、路由逻辑、适配器测试通过，架构验证成功。

## 安全保障

1. **RESEARCH_ONLY 模式锁定**: 配置文件强制 `research_only_mode=True`
2. **QMT 禁用**: `qmt_enabled` 和 `auto_trading_enabled` 强制为 False
3. **风险引擎优先**: 决策只提供建议权重，风险引擎仍是最终约束
4. **审计完整**: 每次决策记录 mode/版本/概率/fallback 事件
5. **Fail-closed**: 异常时明确停止，不静默降级

## 参考文献

本实现参考了近期（2024-2025）多任务 Transformer 金融预测研究：

- [Multi-Layer Hybrid MTL for Stock Prediction](https://arxiv.org/html/2501.09760) (2025)
- [Multi-Sensor Temporal Fusion Transformer](https://www.mdpi.com/1424-8220/25/3/976) (2025)
- [Chinese Stock Prediction Multi-Modal Framework](https://arxiv.org/abs/2501.16621) (2025)
- [Market-Guided Stock Transformer (MASTER)](https://arxiv.org/html/2312.15235v1) (2023)

## 总结

核心架构已完成并通过测试验证。统一决策协议、模式路由、Legacy/Jev 双模式框架就绪。
下一步应优先实现 Legacy Provider 真实集成和 Jev 模型训练，验证端到端流程可行性。
