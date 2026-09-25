# Jev 模型技术文档

## 概述

Jev 是 A 股 AI 投研系统的核心决策模型，采用 Transformer 架构实现多任务学习，能够基于历史 K 线、基本面、情绪等数据预测股票短期走势并生成交易决策。

**版本**: v2.2.0  
**更新日期**: 2026-09-20  
**架构**: Multi-task Transformer  

---

## 核心特性

### 1. 自动训练机制

Jev 模型内置自动训练功能，每周根据最新数据自动更新：

- **数据源**: 真实大盘走向（沪深 300、中证 500、中证 1000）+ 自选股 K 线
- **训练周期**: 每 7 天自动触发一次训练任务
- **数据窗口**: 
  - 训练集：过去 2 年历史数据
  - 验证集：近 60 天数据
  - 测试集：近 30 天数据
- **PIT 约束**: 严格保证时点正确性，防止未来信息泄漏

### 2. 多任务预测

Jev 模型同时预测 6 个任务：

| 任务 | 输出类别 | 说明 |
|------|---------|------|
| `direction_1d` | UP/FLAT/DOWN (3类) | 1日方向预测 |
| `direction_5d` | UP/FLAT/DOWN (3类) | 5日方向预测 |
| `up_over_3pct_5d` | YES/NO (2类) | 5日内是否涨超3% |
| `action` | BUY/HOLD/SELL (3类) | 推荐交易动作 |
| `risk` | LOW/MEDIUM/HIGH (3类) | 风险等级评估 |
| `position` | 0/10/20/30/50/70/100 (7类) | 推荐仓位比例 |

### 3. Transformer 架构

```
输入 (batch, seq_len=60, features=128)
  ↓
Input Projection (Linear)
  ↓
Positional Encoding
  ↓
Transformer Encoder (4层, 8头注意力)
  ↓
6个独立任务头 (全连接层)
  ↓
输出: 6个任务的 logits
```

**超参数**:
- `d_model`: 256 (模型维度)
- `nhead`: 8 (注意力头数)
- `num_encoder_layers`: 4
- `dim_feedforward`: 1024
- `dropout`: 0.1
- `max_seq_len`: 60 (最大序列长度)

---

## 特征工程

### 输入特征 (128维)

#### 技术特征 (~60维)
- OHLCV 基础数据：开盘价、最高价、最低价、收盘价、成交量、成交额
- 均线系统：MA5、MA10、MA20、MA60
- 动量指标：RSI、MACD、KDJ
- 波动率：ATR、布林带宽度
- 量价关系：量比、换手率、资金流向

#### 基本面特征 (~40维)
- 估值指标：PE、PB、PS、PCF
- 盈利能力：ROE、ROA、净利润增长率
- 成长性：营收增长率、净利润同比
- 财务健康：资产负债率、流动比率
- 分红：股息率、分红比例

#### 情绪特征 (~28维)
- 新闻情绪：正面/负面新闻数量、情绪分数
- 公告事件：重大事件标记、公告类型
- 市场情绪：大盘环境、行业走势
- 板块热度：所属板块涨跌幅、资金流入

### 标签生成

#### 方向标签
```python
# 1日/5日方向
return_1d = (close_t+1 - close_t) / close_t
if return_1d > 0.005:
    direction_1d = UP (2)
elif return_1d < -0.005:
    direction_1d = DOWN (0)
else:
    direction_1d = FLAT (1)
```

#### 涨幅标签
```python
# 5日内是否涨超3%
max_return_5d = max((close_t+i - close_t) / close_t for i in range(1, 6))
up_over_3pct_5d = YES (1) if max_return_5d >= 0.03 else NO (0)
```

#### 动作与仓位映射
```python
# 动作决策
if direction_5d == UP and up_over_3pct_5d == YES and risk == LOW:
    action = BUY (2)
    position = 50-100
elif direction_5d == DOWN or risk == HIGH:
    action = SELL (0)
    position = 0-10
else:
    action = HOLD (1)
    position = 20-50
```

---

## 训练流程

### 1. 数据集生成

```bash
# 手动生成数据集
python -m ashare_ai.cli train-jev generate-dataset \
  --bundle-dir data/lake/canonical_daily_bundles \
  --output-dir data/lake/jev_training_data/2026-09-20 \
  --train-start 2024-09-20 \
  --train-end 2026-07-20 \
  --val-start 2026-07-20 \
  --val-end 2026-08-20 \
  --test-start 2026-08-20 \
  --test-end 2026-09-19
```

### 2. 模型训练

```bash
# 训练 Jev 模型
python -m ashare_ai.cli train-jev train \
  --dataset-dir data/lake/jev_training_data/2026-09-20 \
  --checkpoint-dir data/models/jev/v2.2.0 \
  --d-model 256 \
  --nhead 8 \
  --num-layers 4 \
  --batch-size 256 \
  --lr 1e-4 \
  --epochs 100 \
  --device auto
```

### 3. 模型评估

```bash
# 评估测试集性能
python -m ashare_ai.cli train-jev evaluate \
  --checkpoint data/models/jev/v2.2.0/epoch_best.pt \
  --dataset-dir data/lake/jev_training_data/2026-09-20 \
  --split test
```

### 4. 自动训练（推荐）

系统会在每周日凌晨 3:00 自动触发训练任务，无需手动干预。

**配置项** (`.env`):
```bash
# Jev 模型配置
JEV_MODEL_DIR=data/models/jev
JEV_MODEL_VERSION=auto_2026-09-20
JEV_DEVICE=auto
DECISION_MODE=jev
DECISION_FALLBACK_ENABLED=true
```

---

## 损失函数

多任务加权交叉熵损失：

```python
loss_total = Σ (w_task * CrossEntropyLoss(logits_task, labels_task))

# 任务权重
task_weights = {
    "direction_1d": 1.0,
    "direction_5d": 1.0,
    "up_over_3pct_5d": 1.0,
    "action": 2.0,        # 更重要
    "risk": 1.5,
    "position": 1.5,
}
```

---

## 优化器与调度器

- **优化器**: AdamW (weight_decay=1e-5)
- **学习率**: 1e-4 (base)
- **调度器**: Cosine Annealing with Warmup
  - Warmup steps: 1000
  - T_max: num_epochs
- **梯度裁剪**: max_norm=1.0

---

## 性能指标

### 验证集表现 (参考)

| 任务 | Accuracy | 说明 |
|------|----------|------|
| direction_1d | ~55% | 1日方向准确率 |
| direction_5d | ~58% | 5日方向准确率 |
| up_over_3pct_5d | ~72% | 涨超3%识别准确率 |
| action | ~60% | 动作推荐准确率 |
| risk | ~65% | 风险评估准确率 |
| position | ~50% | 仓位预测准确率 |

**注意**: 实际性能受市场环境、数据质量影响较大。

---

## API 使用

### 1. 单个预测

```bash
POST /api/v1/decision/predict
Content-Type: application/json

{
  "symbol": "000001.SZ",
  "trading_date": "2026-09-20",
  "mode": "jev"
}
```

**响应**:
```json
{
  "decision": {
    "direction_1d": "UP",
    "direction_5d": "UP",
    "up_over_3pct_5d": true,
    "action": "BUY",
    "risk": "MEDIUM",
    "position": 50,
    "confidence": {
      "direction_1d": 0.72,
      "direction_5d": 0.68,
      "action": 0.75
    }
  },
  "status": "success"
}
```

### 2. 批量预测

```bash
POST /api/v1/decision/batch
Content-Type: application/json

{
  "symbols": ["000001.SZ", "600000.SH", "000858.SZ"],
  "trading_date": "2026-09-20",
  "mode": "jev"
}
```

### 3. 查询决策模式

```bash
GET /api/v1/decision/mode
```

### 4. 切换决策模式

```bash
POST /api/v1/decision/mode
Content-Type: application/json

{
  "mode": "jev",
  "fallback_enabled": true
}
```

---

## 模型部署

### 1. 模型目录结构

```
data/models/jev/
├── auto_2026-09-20/          # 自动训练模型
│   ├── config.json           # 模型配置
│   ├── epoch_best.pt         # 最佳 checkpoint
│   ├── epoch_final.pt        # 最终 checkpoint
│   └── training_history.json # 训练历史
├── manual_v2.2.0/            # 手动训练模型
│   └── ...
└── production/               # 生产模型（符号链接）
    └── ...
```

### 2. 模型版本管理

```bash
# 列出所有模型
python -m ashare_ai.cli train-jev list-checkpoints \
  --checkpoint-dir data/models/jev

# 切换生产模型
ln -sf auto_2026-09-20 data/models/jev/production
```

---

## 与 Legacy 模型对比

| 特性 | Legacy 模型 | Jev 模型 |
|------|------------|---------|
| 架构 | 规则+评分公式 | Transformer 神经网络 |
| 训练 | 无需训练 | 自动训练/手动训练 |
| 特征 | 35项固定特征 | 128维动态特征 |
| 输出 | 单一评分 | 6任务多维决策 |
| 可解释性 | 高（规则透明） | 中（注意力可视化） |
| 准确率 | 稳定但有限 | 更高但依赖数据质量 |
| 适用场景 | 稳健型策略 | 激进型策略 |

**推荐策略**: 启用 `fallback_enabled=true`，Jev 失败时自动回退到 Legacy。

---

## 常见问题

### Q1: 如何强制重新训练模型？

```python
from ashare_ai.orchestration.jev_training_jobs import schedule_jev_auto_training

await schedule_jev_auto_training(db, force=True)
```

### Q2: 模型预测失败怎么办？

检查以下配置：
1. `DECISION_MODE=jev` 已设置
2. `JEV_MODEL_VERSION` 指向有效模型
3. 模型文件完整（config.json + checkpoint）
4. 启用 fallback: `DECISION_FALLBACK_ENABLED=true`

### Q3: 如何查看模型注意力权重？

```python
# TODO: 实现注意力可视化工具
from ashare_ai.decision_training.visualization import plot_attention

plot_attention(model, input_sequence, output_path="attention.png")
```

### Q4: 训练数据不足怎么办？

- 确保至少有 2 年历史数据
- 扩大股票池（增加自选股）
- 降低 `min_history_days` 参数

---

## 未来规划

- [ ] 增加技术指标特征（RSI、MACD 等）
- [ ] 支持多时间尺度预测（日线、周线）
- [ ] 注意力权重可视化
- [ ] 模型蒸馏（加速推理）
- [ ] 增量训练（无需全量重训）
- [ ] A/B 测试框架

---

## 参考文献

1. Vaswani et al., "Attention is All You Need", NeurIPS 2017
2. Caruana, "Multitask Learning", Machine Learning 1997
3. Zhang et al., "AlphaStock: A Buying-Winners-and-Selling-Losers Investment Strategy using Interpretable Deep Reinforcement Attention Networks", KDD 2019

---

**维护者**: A 股 AI 投研团队  
**最后更新**: 2026-09-20
