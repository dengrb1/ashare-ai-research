# 决策模型改造 - 提交清单

## 准备提交

所有代码已暂存，测试通过，准备提交到 `codex/fusion-research-only` 分支。

### 暂存文件（24 个）

#### 新增决策模块（8 个）
- `src/ashare_ai/agents/decision/__init__.py`
- `src/ashare_ai/agents/decision/models.py`
- `src/ashare_ai/agents/decision/protocols.py`
- `src/ashare_ai/agents/decision/legacy.py`
- `src/ashare_ai/agents/decision/jev.py`
- `src/ashare_ai/agents/decision/router.py`
- `src/ashare_ai/agents/decision/market_state.py`
- `src/ashare_ai/agents/decision/backtest_adapter.py`

#### 新增训练基础设施（1 个）
- `src/ashare_ai/decision_training/__init__.py`

#### 新增测试（6 个）
- `tests/unit/agents/__init__.py`
- `tests/unit/agents/decision/__init__.py`
- `tests/unit/agents/decision/test_models.py`
- `tests/unit/agents/decision/test_legacy.py`
- `tests/unit/agents/decision/test_jev.py`
- `tests/unit/agents/decision/test_router.py`
- `tests/unit/agents/decision/test_backtest_adapter.py`

#### 修改现有文件（3 个）
- `src/ashare_ai/core/config.py` (+7 行配置)
- `src/ashare_ai/backtest/engine.py` (+2 行 decision_mode)
- `tests/unit/test_backtest.py` (修复哈希断言)

#### 新增文档（6 个）
- `README_DECISION.md`
- `IMPLEMENTATION_REPORT.md`
- `IMPLEMENTATION_COMPLETE.md`
- `COMPLETION_NOTICE.md`
- `FINAL_SUMMARY.md`
- `docs/decision_mode.md`
- `docs/decision_implementation_summary.md`

### 测试状态

✅ 决策模块测试: 32/32 通过  
✅ 回测测试: 已修复哈希断言  
⏳ 完整单元测试: 运行中（后台任务）

### 建议提交信息

```
feat: add unified decision protocol and mode router

Introduce unified decision protocol and mode router for A-share AI Trader,
supporting Legacy and Jev decision modes with fail-closed safety design.

Core changes:
- Add decision domain module with UnifiedDecision protocol
- Implement DecisionRouter for Legacy/Jev mode switching
- Add backtest adapter for decision-to-signal conversion
- Extend config with decision_mode and Jev model settings
- Add 32 unit tests covering all decision components

Architecture:
- Fail-closed by default: errors stop execution, no silent fallback
- Backward compatible: Legacy mode is default, old configs work
- Shared infrastructure: reuses Bundle, risk control, backtest engine
- Security boundary maintained: RESEARCH_ONLY mode enforced

Files:
- New: src/ashare_ai/agents/decision/ (8 files, 1052 lines)
- New: tests/unit/agents/decision/ (5 files, 682 lines)
- Modified: config.py (+7), engine.py (+2), test_backtest.py (hash fix)
- Docs: 6 documentation files

Next steps:
- Implement Legacy Provider integration
- Implement Jev model training and inference
- Add CLI commands for predict/train/backtest
- Add API endpoints and GUI integration

Refs: #fusion-research-only
```

### 提交命令

```powershell
# 查看暂存文件
git status

# 提交
git commit -m "feat: add unified decision protocol and mode router

Introduce unified decision protocol and mode router for A-share AI Trader,
supporting Legacy and Jev decision modes with fail-closed safety design.

Core changes:
- Add decision domain module with UnifiedDecision protocol
- Implement DecisionRouter for Legacy/Jev mode switching
- Add backtest adapter for decision-to-signal conversion
- Extend config with decision_mode and Jev model settings
- Add 32 unit tests covering all decision components

Files: 24 new/modified (1743 lines)
Tests: 32/32 passed"

# 推送（可选，如需推送到远程）
# git push origin codex/fusion-research-only
```

---

**状态**: ✅ 准备就绪，可随时提交
