# Deterministic Factor Decisions

新决策统一使用版本化的 `factor` 公式，默认版本为 `factor-v1.0.0`。
`legacy` 仅是兼容输入别名，仍然返回 `mode=factor`；`jev` 已移除，新请求会返回弃用错误。

## 配置

```bash
DECISION_MODE=factor
DECISION_FALLBACK_ENABLED=true
FACTOR_CONFIDENCE_THRESHOLD=0.60
DECISION_SYSTEM2_ENABLED=true
```

FactorDecisionProvider 只读取冻结的点内时间市场状态和 CompositeScore，使用基本面、技术面、情绪面、质量分、事件风险乘数以及冻结市场指数快照生成结果。相同输入与相同 FactorGenome 始终产生相同输出。

每个结果记录 `formula_version`、`parameter_sha256`、`dataset_sha256` 和 `input_manifest_hash`。历史 `legacy` 与 `jev` JSON 仍可由 UnifiedDecision 反序列化，用于只读展示和回测。

## 周度因子进化

单一 job-worker 每周最多运行一次。任务使用固定训练、验证和测试窗口，从冻结 CanonicalDailyBundle 构建点内数据集；LLM 最多提出一次权重和阈值候选，所有候选经过 Pydantic 校验和确定性 walk-forward 回测。至少 60 个样本、最大回撤 20%、换手率 20% 和相对 active 策略的改善门槛必须同时满足。

通过门槛的候选保存为 `CANDIDATE`，不会自动改变 active 版本。管理员可在设置页或 `/api/v1/strategies/evolution` 下查看、批准、拒绝和回滚。批准只切换 active 指针，不改写历史研究结果。

System-2 只生成低置信度风险诊断，不修改因子评分、动作或仓位。
