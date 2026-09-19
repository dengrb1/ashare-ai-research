# 决策模型改造完成通知

## 状态：✅ 核心架构实施完成

已成功完成 A-share AI Trader 决策模型增量改造的核心架构实施。

### 完成内容

#### 1. 统一决策协议
- ✅ UnifiedDecision、DecisionProbabilities、MarketState 模型
- ✅ 严格概率和校验、时间戳校验、OHLC 校验
- ✅ 6 个任务头输出（方向 1d/5d、涨幅、动作、风险、仓位）

#### 2. 决策模式路由
- ✅ DecisionRouter 支持 Legacy/Jev 双模式
- ✅ Fail-closed 默认（异常明确报错）
- ✅ 可选 Fallback（显式开启后才回退）

#### 3. Provider 实现
- ✅ LegacyDecisionProvider（骨架）
- ✅ JevDecisionProvider（骨架，支持 PyTorch）

#### 4. 回测集成
- ✅ decision_to_signal() 适配器
- ✅ Position 枚举到 target_weight 映射
- ✅ BacktestConfig.decision_mode 字段

#### 5. 配置扩展
- ✅ decision_mode: "legacy" | "jev"
- ✅ decision_fallback_enabled
- ✅ jev_model_dir, jev_model_version, jev_device

#### 6. 完整测试
- ✅ 32 个单元测试全部通过
- ✅ 覆盖协议、路由、适配器、模式切换

### 代码统计
- 新增决策模块：8 个文件，1,052 行
- 新增测试：5 个文件，682 行
- 修改现有文件：2 个文件，9 行
- 文档：4 个文件

### 已知问题

**回测哈希变化**：`BacktestConfig` 新增 `decision_mode` 字段后，`output_hash` 发生变化。

原因：`BacktestConfig` 是 frozen model，字段变更会影响哈希。

影响：`test_signal_is_executed_next_day_and_t1_sell_waits_until_following_session` 的硬编码哈希断言失败。

解决方案：
1. 更新测试中的硬编码哈希值（推荐）
2. 或将 `decision_mode` 从哈希计算中排除（需修改 BacktestConfig）

### 下一步

#### 优先级 1：修复回测测试
```python
# 更新 tests/unit/test_backtest.py:221 的期望哈希
assert result.output_hash == "d5145c674122f3e0cff74ae69d174c620d7d2f3a35fffa86bf0f6cffeda37429"
```

#### 优先级 2：Legacy Provider 集成
- 连接 orchestration.builtin
- 从 CompositeScore 转换为 UnifiedDecision

#### 优先级 3：CLI 命令
- `ashare-ai predict --mode legacy|jev`
- `ashare-ai backtest --mode legacy|jev`

#### 优先级 4：Jev 模型实现
- 数据集生成和标签生成
- Transformer 架构实现
- 训练循环和 checkpoint 管理

### 文档
- ✅ README_DECISION.md
- ✅ IMPLEMENTATION_REPORT.md
- ✅ IMPLEMENTATION_COMPLETE.md
- ✅ docs/decision_mode.md
- ✅ docs/decision_implementation_summary.md

### 验证

```powershell
# 决策模块测试（32/32 通过）
$env:PYTHONPATH='src'
python -m pytest tests/unit/agents/decision/ -v

# 结果
======================== 32 passed, 1 warning in 0.63s ========================
```

### 安全保障
✅ RESEARCH_ONLY 模式保持  
✅ QMT 保持禁用  
✅ 不新增真实交易路径  
✅ 风险引擎仍是最终约束  

---
**任务状态**: 核心架构完成，待修复回测哈希测试
