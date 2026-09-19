# 决策模式集成实施进度

## ✅ 已完成

### 核心架构（第一阶段）
- ✅ 统一决策协议（UnifiedDecision、DecisionProbabilities、MarketState）
- ✅ 决策模式路由器（DecisionRouter）
- ✅ Legacy Provider 骨架
- ✅ Jev Provider 骨架
- ✅ 回测适配器（decision_to_signal）
- ✅ 配置扩展（decision_mode 等 7 个字段）
- ✅ 完整测试（32/32 通过）

### Legacy Provider 集成（第二阶段 - 进行中）
- ✅ LegacyDecisionProvider 支持 CompositeScore 输入
- ✅ convert_composite_score_to_decision() 转换函数
- ✅ 评分到动作/风险/仓位的映射规则
- ✅ orchestration/decision_integration.py 集成层
  - generate_decision_from_composite_score()
  - generate_decision_for_symbol()
  - batch_generate_decisions()
- ✅ CLI 命令骨架（cli/decision_commands.py）
  - predict - 单个决策预测
  - batch - 批量预测
  - info - 查看配置
  - list-models - 列出模型
- ✅ API 端点骨架（api/decision_endpoints.py）
  - POST /api/v1/decision/predict
  - POST /api/v1/decision/batch
  - GET /api/v1/decision/mode
  - POST /api/v1/decision/mode
  - GET /api/v1/decision/models
- ✅ 集成测试（test_integration.py）
  - LegacyProvider 与 CompositeScore 集成测试
  - 转换函数测试
  - 评分边界测试

## 🔧 待完成

### Legacy Provider 真实集成
- [ ] 连接 orchestration.builtin.run_research_for_symbol()
- [ ] 从 builtin pipeline 获取 CompositeScore
- [ ] 在 builtin workflow 中调用 generate_decision_from_composite_score()
- [ ] 实现 CLI 命令的 Bundle 加载逻辑
- [ ] 实现 API 端点的 Bundle 加载逻辑
- [ ] 集成到主 CLI 入口（如果存在）
- [ ] 集成到主 API 路由（app.py）

### Jev 模型实现（第三阶段）
- [ ] 数据集生成（从 CanonicalDailyBundle）
- [ ] 标签生成（多任务目标）
- [ ] 时序切分（walk-forward）
- [ ] Transformer 架构实现
- [ ] 训练循环和 checkpoint 管理
- [ ] 推理优化

### 回测集成（第四阶段）
- [ ] BacktestEngine 调用 DecisionRouter
- [ ] Legacy vs Jev 对比报告
- [ ] CLI: ashare-ai backtest --mode legacy|jev

### GUI 集成（第五阶段）
- [ ] 系统设置页添加 decision_mode 切换
- [ ] 模型管理页面
- [ ] 决策结果展示

## 📊 进度统计

| 阶段 | 完成度 | 文件数 |
|------|--------|--------|
| 核心架构 | 100% | 23 |
| Legacy 集成 | 60% | 4 |
| Jev 模型 | 0% | 0 |
| 回测集成 | 0% | 0 |
| GUI 集成 | 0% | 0 |

**总体进度**: 约 35%

## 📝 当前会话新增文件

1. `src/ashare_ai/orchestration/decision_integration.py` - 集成层
2. `src/ashare_ai/cli/decision_commands.py` - CLI 命令
3. `src/ashare_ai/api/decision_endpoints.py` - API 端点
4. `tests/unit/agents/decision/test_integration.py` - 集成测试

已修改：
- `src/ashare_ai/agents/decision/legacy.py` - 添加 CompositeScore 支持

## 🎯 下一步（等待用户指示）

建议按优先级实施：

1. **Legacy Provider 真实集成**（1-2 天）
   - 在 orchestration.builtin 中调用决策模块
   - 实现端到端流程验证

2. **CLI/API 完善**（0.5-1 天）
   - 连接 Bundle 加载逻辑
   - 集成到主入口

3. **Jev 模型实现**（1-2 周）
   - 数据集和训练流程

---

**状态**: Legacy Provider 集成层和接口已完成，等待连接现有 builtin pipeline
