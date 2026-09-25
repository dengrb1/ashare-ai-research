# 策略优化指南

## 概述

本文档介绍如何在 A 股 AI 投研系统中优化和配置交易策略。系统支持多层次的策略定制，从简单的规则调整到复杂的模型融合。

**更新日期**: 2026-09-20  
**版本**: v2.2.0

---

## 策略层级

### 1. 评分公式优化 (Scoring Formula)

最基础的策略层，通过调整权重改变决策逻辑。

#### 当前公式: `composite-35-35-20-10-dividend-news-market-v3`

```
综合评分 = 
  基本面 (35%) × 基本面子评分 +
  技术面 (35%) × 技术面子评分 +
  事件情绪 (20%) × 事件情绪子评分 +
  质量置信 (10%) × 质量置信子评分 +
  分红调整 × 分红倍数 +
  事件风险 × 风险乘数 +
  大盘环境 × 市场乘数
```

#### 权重调整示例

**保守策略** (降低风险):
```json
{
  "fundamental_weight": 0.45,
  "technical_weight": 0.30,
  "sentiment_weight": 0.15,
  "quality_weight": 0.10,
  "market_regime_multiplier": 0.8,
  "event_risk_multiplier": 1.2
}
```

**激进策略** (追求收益):
```json
{
  "fundamental_weight": 0.25,
  "technical_weight": 0.45,
  "sentiment_weight": 0.20,
  "quality_weight": 0.10,
  "market_regime_multiplier": 1.2,
  "event_risk_multiplier": 0.8
}
```

**平衡策略** (兼顾风险收益):
```json
{
  "fundamental_weight": 0.35,
  "technical_weight": 0.35,
  "sentiment_weight": 0.20,
  "quality_weight": 0.10,
  "market_regime_multiplier": 1.0,
  "event_risk_multiplier": 1.0
}
```

### 2. 决策模式选择 (Decision Mode)

#### Legacy 模式 (规则评分)

优点:
- 完全透明，每一步都可追溯
- 稳定性强，不依赖数据质量
- 易于解释，便于风险管理
- 快速响应，无需训练

缺点:
- 规则固定，难以适应市场变化
- 特征工程手工劳动量大
- 准确率有上限

**推荐场景**:
- 稳健型策略
- 风险厌恶投资者
- 需要高可解释性
- 市场结构稳定

#### Jev 模式 (神经网络)

优点:
- 自动特征学习，捕捉复杂规律
- 多任务学习，综合决策能力强
- 自动训练，快速适应市场变化
- 准确率更高

缺点:
- 黑盒性质，可解释性较低
- 依赖历史数据质量
- 需要定期重训
- 过拟合风险

**推荐场景**:
- 激进型策略
- 有充足历史数据
- 需要快速适应
- 可以接受不完全透明

#### 混合模式 (推荐)

```python
# 使用 Jev 预测，Fallback 到 Legacy
decision_mode = "jev"
fallback_enabled = True

# 实现逻辑：
if jev_prediction.confidence >= 0.7:
    use_jev_decision()
else:
    use_legacy_decision()
```

---

## 特征工程优化

### 技术特征增强

#### 1. 动量特征
```python
# RSI (相对强弱指数)
rsi_14 = calculate_rsi(closes, period=14)
rsi_oversold = rsi_14 < 30  # 超卖信号
rsi_overbought = rsi_14 > 70  # 超买信号

# MACD (指数平滑移动平均线收敛发散)
macd_line, signal_line = calculate_macd(closes)
macd_histogram = macd_line - signal_line
macd_bullish = (macd_histogram > 0) & (macd_histogram > macd_histogram[-1])

# KDJ 随机指标
k, d, j = calculate_kdj(high, low, close)
kdj_signal = (k > d) & (k < 80)  # 金叉且未超买
```

#### 2. 波动率特征
```python
# ATR (平均真实波幅)
atr_14 = calculate_atr(high, low, close, period=14)
atr_ratio = atr_14 / close  # 波动率相对水平

# 布林带
bb_upper, bb_mid, bb_lower = calculate_bollinger_bands(closes, period=20, std=2)
bb_squeeze = (bb_upper - bb_lower) / bb_mid  # 波动率压缩
```

#### 3. 成交量特征
```python
# 量比
volume_ratio = volume[-1] / volume[-20:].mean()

# 资金流向 (MFI)
mfi = calculate_money_flow_index(high, low, close, volume)

# OBV (能量潮)
obv = calculate_on_balance_volume(close, volume)
```

### 基本面特征优化

#### 1. 估值指标
```python
# 相对估值
pe_percentile = percentile_rank(pe, period=252)  # 近一年 PE 分位数
pb_percentile = percentile_rank(pb, period=252)

# 估值性价比
value_score = (1 - pe_percentile) + (1 - pb_percentile)
```

#### 2. 盈利质量
```python
# 盈利增长
earnings_growth_yoy = (eps[-4:].sum() - eps[-8:-4].sum()) / eps[-8:-4].sum()
earnings_growth_qoq = eps[-1] / eps[-2] - 1

# 现金流质量
fcf_quality = operating_cash_flow / net_income
```

#### 3. 财务健康度
```python
# 杜邦分析
roe = net_income / equity
roa = net_income / assets
roic = nopat / invested_capital

# 债务管理
debt_ratio = total_debt / total_assets
interest_coverage = ebit / interest_expense
```

### 情绪特征增强

#### 1. 新闻情绪
```python
# 新闻频率
news_frequency = count_news(symbol, period=5)  # 近5天新闻数
news_sentiment = calculate_sentiment_score(news_texts)

# 新闻主题分类
news_topics = {
    "positive": ["上升", "增长", "突破"],
    "negative": ["下降", "风险", "调查"],
    "neutral": ["公告", "会议"]
}
```

#### 2. 公告事件
```python
# 重大公告
major_events = {
    "earnings": 1.2,  # 业绩公告权重系数
    "dividend": 0.8,  # 分红公告
    "acquisition": 1.5,  # 并购重组
    "litigation": -1.5  # 诉讼风险
}

event_impact = sum(major_events.get(event, 0) for event in recent_events)
```

#### 3. 市场情绪
```python
# 大盘环境
market_regime = classify_market(index_returns_1d, index_returns_5d, index_returns_20d)
# RISK_ON: 大盘向上
# NEUTRAL: 大盘横盘
# RISK_OFF: 大盘向下

# 行业轮动
sector_momentum = calculate_sector_returns(symbol_sector)
sector_percentile = percentile_rank(sector_momentum, peer_sectors)
```

---

## 风险管理策略

### 1. 投资组合风险

#### 集中度控制
```python
# 单股占比限制
max_single_position = 0.10  # 单股不超过 10%

# 行业集中度
max_sector_weight = 0.30  # 单行业不超过 30%

# 风险因子暴露
max_factor_exposure = 0.5  # 单因子不超过 50%
```

#### 相关性管理
```python
# 计算投资组合相关性
correlation_matrix = calculate_correlation(returns)

# 去除高相关资产
if correlation > 0.8:
    drop_highly_correlated_asset()
```

### 2. 单股风险

#### 止损设置
```python
# 时间止损
max_holding_days = 30
if days_held > max_holding_days:
    sell_position()

# 价格止损
stop_loss_price = entry_price * 0.95  # 下跌 5% 止损
if current_price < stop_loss_price:
    sell_position()

# 心理止损 (目标收益)
take_profit_price = entry_price * 1.15  # 上涨 15% 止盈
if current_price > take_profit_price:
    sell_position()
```

#### 风险等级映射
```python
risk_level = calculate_risk_level(symbol)

if risk_level == "HIGH":
    position_size = 0.5  # 50% 仓位
    stop_loss = 0.03  # 3% 止损
elif risk_level == "MEDIUM":
    position_size = 1.0  # 100% 仓位
    stop_loss = 0.05  # 5% 止损
else:  # LOW
    position_size = 1.5  # 150% 仓位
    stop_loss = 0.08  # 8% 止损
```

### 3. 市场环境风险

#### 大盘环境调整
```python
market_env = get_market_environment()

if market_env == "RISK_OFF":
    # 高风险时降低权重
    position_multiplier = 0.5
    max_position = 0.05
    stop_loss = 0.02
elif market_env == "NEUTRAL":
    position_multiplier = 1.0
    max_position = 0.10
    stop_loss = 0.05
else:  # RISK_ON
    position_multiplier = 1.3
    max_position = 0.12
    stop_loss = 0.08
```

---

## 交易规则优化

### 1. 买入条件

#### 基础条件
```python
def should_buy(symbol: str) -> bool:
    # 评分达到阈值
    score = get_score(symbol)
    if score < 70:
        return False
    
    # 技术面支持
    direction_1d = get_direction_1d(symbol)
    direction_5d = get_direction_5d(symbol)
    if direction_1d != "UP" or direction_5d != "DOWN":
        return False
    
    # 行情支持
    volume_ratio = get_volume_ratio(symbol)
    if volume_ratio < 1.2:  # 量能不足
        return False
    
    # 风险检查
    if is_limit_up(symbol) or is_limit_down(symbol):
        return False
    
    return True
```

#### 高级条件
```python
def advanced_buy_conditions(symbol: str) -> bool:
    conditions = []
    
    # 条件1: 基本面强劲
    if get_pe_percentile(symbol) < 0.5:  # PE 低于中位数
        conditions.append(0.3)
    
    # 条件2: 技术面突破
    if is_breakout(symbol, period=20):  # 突破 20 日高点
        conditions.append(0.4)
    
    # 条件3: 情绪面积极
    if get_news_sentiment(symbol) > 0.7:
        conditions.append(0.3)
    
    # 条件4: 市场环境友好
    if get_market_environment() == "RISK_ON":
        conditions.append(0.2)
    
    # 综合评分
    total_score = sum(conditions)
    return total_score >= 0.6
```

### 2. 卖出条件

#### 止盈止损
```python
def should_sell(symbol: str, entry_price: float, current_price: float) -> bool:
    pnl_pct = (current_price - entry_price) / entry_price
    
    # 止盈
    if pnl_pct >= 0.15:  # 上涨 15%
        return True
    
    # 止损
    if pnl_pct <= -0.05:  # 下跌 5%
        return True
    
    # 时间止损
    days_held = get_days_held(symbol)
    if days_held > 30:
        return True
    
    return False
```

#### 风险卖出
```python
def risk_sell(symbol: str) -> bool:
    # 评分崩塌
    current_score = get_score(symbol)
    prev_score = get_prev_score(symbol)
    if current_score < prev_score * 0.8:  # 评分下跌 20%
        return True
    
    # 跌穿重要支撑
    if current_price < ma_20 and prev_price > ma_20:
        return True
    
    # 利空事件
    if has_negative_news(symbol):
        return True
    
    # 大盘暴跌
    if get_market_change() < -3.0:  # 大盘跌 3%
        return True
    
    return False
```

---

## 回测与优化

### 1. 回测框架

```python
class BacktestEngine:
    def __init__(self, start_date, end_date, initial_capital=100000):
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.portfolio = {}
        self.metrics = {}
    
    def run(self, strategy: Strategy) -> BacktestResult:
        for trading_date in self.trading_dates:
            # 1. 生成信号
            signals = strategy.generate_signals(trading_date)
            
            # 2. 执行交易
            self.execute_trades(signals)
            
            # 3. 计算收益
            daily_return = self.calculate_daily_return()
            
            # 4. 记录指标
            self.record_metrics(daily_return)
        
        return self.generate_report()
```

### 2. 性能指标

| 指标 | 说明 | 目标 |
|------|------|------|
| 总收益率 (Total Return) | 投资期间总收益 | 15%+ |
| 年化收益率 (CAGR) | 年平均复合收益率 | 12%+ |
| 夏普比率 (Sharpe Ratio) | 风险调整后收益 | 1.0+ |
| 最大回撤 (Max Drawdown) | 最大连续下跌 | -20% 以内 |
| 赢率 (Win Rate) | 盈利交易占比 | 55%+ |
| 盈亏比 (Profit Factor) | 盈利总额/亏损总额 | 2.0+ |
| 卡玛比率 (Calmar Ratio) | 年化收益/最大回撤 | 0.5+ |

### 3. 参数优化

#### 网格搜索
```python
def grid_search(param_grid: dict) -> dict:
    best_params = None
    best_sharpe = -np.inf
    
    for params in itertools.product(*param_grid.values()):
        param_dict = dict(zip(param_grid.keys(), params))
        strategy = Strategy(**param_dict)
        result = backtest_engine.run(strategy)
        
        if result.sharpe_ratio > best_sharpe:
            best_sharpe = result.sharpe_ratio
            best_params = param_dict
    
    return best_params
```

#### 随机搜索
```python
def random_search(param_distributions: dict, n_iter=100) -> dict:
    best_params = None
    best_sharpe = -np.inf
    
    for _ in range(n_iter):
        params = {k: np.random.choice(v) for k, v in param_distributions.items()}
        strategy = Strategy(**params)
        result = backtest_engine.run(strategy)
        
        if result.sharpe_ratio > best_sharpe:
            best_sharpe = result.sharpe_ratio
            best_params = params
    
    return best_params
```

---

## 策略监控

### 1. 实时监控指标

```python
class StrategyMonitor:
    def __init__(self):
        self.daily_pnl = []
        self.winning_trades = 0
        self.losing_trades = 0
    
    def update(self, date: date, daily_return: float):
        self.daily_pnl.append(daily_return)
        
        # 计算滚动指标
        sharpe = calculate_sharpe(self.daily_pnl[-252:])
        max_dd = calculate_max_drawdown(self.daily_pnl)
        win_rate = self.winning_trades / (self.winning_trades + self.losing_trades)
        
        # 预警
        if sharpe < 0.5:
            logger.warning(f"Sharpe ratio dropped to {sharpe}")
        if max_dd < -0.3:
            logger.warning(f"Max drawdown exceeded {max_dd}")
        if win_rate < 0.4:
            logger.warning(f"Win rate dropped to {win_rate}")
```

### 2. A/B 测试

```python
# 对比两个策略
strategy_a = LegacyStrategy(weights=[0.35, 0.35, 0.20, 0.10])
strategy_b = JevStrategy(model_version="auto_2026-09-20")

result_a = backtest_engine.run(strategy_a)
result_b = backtest_engine.run(strategy_b)

# 统计检验
t_stat, p_value = scipy.stats.ttest_rel(result_a.returns, result_b.returns)

if p_value < 0.05:
    logger.info(f"Strategy B is significantly better (p={p_value:.4f})")
else:
    logger.info(f"No significant difference (p={p_value:.4f})")
```

---

## 最佳实践

### 1. 策略开发流程

```
1. 想法验证 (Idea Validation)
   ↓
2. 原型开发 (Prototype)
   ↓
3. 回测优化 (Backtest & Optimize)
   ↓
4. 纸盘测试 (Paper Trading)
   ↓
5. 小额实盘 (Live Trading - Small)
   ↓
6. 全额运行 (Live Trading - Full)
```

### 2. 风险控制清单

- [ ] 单股仓位 ≤ 10%
- [ ] 单行业仓位 ≤ 30%
- [ ] 最大回撤控制 ≤ 20%
- [ ] 止损/止盈规则已设定
- [ ] 大盘环境检查已启用
- [ ] 基金经理审核已通过
- [ ] 紧急熔断机制已配置

### 3. 常见陷阱

| 陷阱 | 症状 | 解决方案 |
|------|------|--------|
| 过拟合 (Overfitting) | 回测优异，实盘差 | 使用 Walk-forward 验证 |
| 前瞻性偏差 | 使用未来数据 | 严格 PIT 约束检查 |
| 生存偏差 | 忽略退市股票 | 包含历史退市数据 |
| 交易成本忽视 | 低估滑点和手续费 | 参数中加入成本 |
| 样本量过少 | 统计不显著 | 至少 3 年数据 |

---

## 与优秀项目的对标

### 参考项目特性

#### 1. 阿尔法研究平台
- **特点**: 多因子模型 + 机器学习
- **启示**: 特征工程很关键，需要不断迭代

#### 2. 量化宽客
- **特点**: 规则清晰 + 风控严格
- **启示**: 风险管理比收益更重要

#### 3. 米筐科技
- **特点**: 实时数据 + 高效回测
- **启示**: 基础设施和速度是竞争力

### 本项目优势

✅ 自动化程度高 (自动训练、自动更新)  
✅ 多模式融合 (Legacy + Jev)  
✅ 可追溯性强 (PIT 约束、审计日志)  
✅ 易于理解 (规则透明、可视化)

### 改进方向

- [ ] 增加因子库，支持自定义因子
- [ ] 实现多策略组合
- [ ] 支持期权、期货等衍生品
- [ ] 增加实时风控引擎
- [ ] 优化回测效率

---

**最后更新**: 2026-09-20  
**维护者**: A 股 AI 投研团队
