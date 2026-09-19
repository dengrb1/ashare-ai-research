# Decision Mode Configuration

统一决策协议和模式路由已实现，支持 Legacy 和 Jev 两种模式。

## 配置选项

在 `.env` 或环境变量中配置：

```bash
# 决策模式：legacy 或 jev
DECISION_MODE=legacy

# 是否启用 fallback（Jev 失败时回退到 Legacy）
DECISION_FALLBACK_ENABLED=false

# Jev 模型配置
JEV_MODEL_DIR=data/models/jev
JEV_MODEL_VERSION=jev-baseline-v1
JEV_DEVICE=auto  # auto, cpu, cuda
# JEV_CHECKPOINT=  # 可选：指定具体 checkpoint 路径
```

## 默认行为

- 默认模式：`legacy`
- Fallback 默认关闭
- Jev 模型异常时默认停止并返回错误，不静默回退
- 只有显式开启 `DECISION_FALLBACK_ENABLED=true` 才允许回退

## 使用示例

### Python API

```python
from ashare_ai.agents.decision import create_decision_router, MarketState
from datetime import date, datetime, timezone

# 创建路由器
router = create_decision_router(
    mode="legacy",
    fallback_enabled=False,
)

# 构建市场状态
market_state = MarketState(
    symbol="600000.SH",
    trading_date=date(2026, 7, 17),
    available_at=datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc),
    open=10.0,
    high=10.5,
    low=9.8,
    close=10.2,
    volume=1000000.0,
    amount=10000000.0,
)

# 生成决策
decision = await router.predict(market_state)

print(f"Action: {decision.action}, Risk: {decision.risk}, Position: {decision.position}")
print(f"Mode: {decision.mode}, Model: {decision.model_version}")
```

### 回测集成

```python
from ashare_ai.agents.decision.backtest_adapter import decision_to_signal

# 将决策转换为回测信号
signal = decision_to_signal(
    decision=decision,
    snapshot_hash="...",
    industry_code="801010",
)

# 信号可直接用于 BacktestEngine
```

## 训练 Jev 模型

```powershell
# 训练模型（骨架实现，需补充完整训练流程）
ashare-ai train-jev --dataset path/to/dataset --output data/models/jev/jev-baseline-v1
```

## 审计

每次决策都会记录：
- mode（legacy/jev）
- model_version
- 全部概率分布
- action/risk/position
- input_manifest_hash
- fallback 是否发生

## 安全边界

- 保持 `RESEARCH_ONLY` 模式约束
- 不新增真实交易路径
- QMT 保持禁用
- 所有模式都受现有风险控制约束
