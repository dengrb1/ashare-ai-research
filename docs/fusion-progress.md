# 研究只读融合进度

## Phase 0: 建立基线 ✅

已完成：
1. ✅ 在 `main` 分支验证现有系统状态
2. ✅ 盘点 QMT 仓库的交易入口、Gateway、Console、脚本
3. ✅ 确认数据库契约（PostgreSQL + Redis + Celery）
4. ✅ 确认现有模块结构和 API 契约

当前仓库状态：
- 主仓库：`F:\code\ashare-ai-src`
- QMT 来源：`F:\code\qmt`（只读参考）
- 数据库：PostgreSQL (5432), Redis (6379)
- 现有模块：
  - `src/ashare_ai/api/` - FastAPI 应用
  - `src/ashare_ai/features/` - 特征工程
  - `src/ashare_ai/scoring/` - 评分模型
  - `src/ashare_ai/market/` - 市场数据
  - `src/ashare_ai/backtest/` - 回测引擎
  - `src/ashare_ai/portfolio/` - 组合管理
  - `src/ashare_ai/reports/` - 报告生成
  - `src/ashare_ai/agents/` - AI Agent
  - `src/ashare_ai/orchestration/` - 任务编排

## Phase 1: 完成研究只读闭锁 ✅

### 1.1 配置层加固 ✅

修改文件：`src/ashare_ai/core/config.py`

添加的强制配置：
```python
@field_validator("research_only_mode")
@classmethod
def lock_research_only_mode(cls, v: bool) -> bool:
    """Research-only mode cannot be disabled."""
    return True

@field_validator("qmt_enabled")
@classmethod
def lock_qmt_disabled(cls, v: bool) -> bool:
    """QMT integration is permanently disabled in research-only mode."""
    return False

@field_validator("auto_trading_enabled")
@classmethod
def lock_auto_trading_disabled(cls, v: bool) -> bool:
    """Auto trading is permanently disabled in research-only mode."""
    return False

@field_validator("execution_mode")
@classmethod
def lock_execution_mode(cls, v: str) -> str:
    """Execution mode is locked to RESEARCH_ONLY."""
    return "RESEARCH_ONLY"
```

验证：
- ✅ 环境变量无法覆盖这些设置
- ✅ 配置对象始终返回安全值
- ✅ 所有4个字段都有 field_validator

### 1.2 Health Endpoint 状态暴露 ✅

已确认 `src/ashare_ai/api/app.py` health endpoint 返回：
```python
HealthResponse(
    status="ok" if database == "ok" else "degraded",
    version=__version__,
    database=database,
    git_sha=_api_settings.git_sha,
    qmt_enabled=False,
    auto_trading_enabled=False,
    execution_mode="RESEARCH_ONLY",
)
```

### 1.3 测试覆盖 ✅

新增文件：`tests/test_research_only_mode.py`

测试覆盖：
1. ✅ `test_research_only_mode_is_enabled` - 配置默认值
2. ✅ `test_qmt_cannot_be_enabled` - QMT 无法启用
3. ✅ `test_auto_trading_cannot_be_enabled` - 自动交易无法启用
4. ✅ `test_execution_mode_is_locked` - 执行模式锁定
5. ✅ `test_health_endpoint_exposes_research_mode` - Health 响应模型

所有测试通过：
```
tests/test_research_only_mode.py .....  [100%]
======================== 5 passed, 1 warning in 0.23s =========================
```

### 1.4 API 端点审查 ✅

已完成审查：
- ✅ `src/ashare_ai/api/app.py` - 无真实下单端点
- ✅ 无 `routes/` 子目录 - 所有端点在 app.py
- ✅ Trade Plan 端点是组合优化建议，不是真实交易
- ✅ 确认无 xtquant/MiniQMT 导入
- ✅ pyproject.toml 无 xtquant 依赖

关键发现：
- `/api/v1/reports/{report_id}/trade-plans` 提交的是**模拟交易方案**
- Worker `execute_trade_plan_job()` 执行回测优化，不下单
- 通知文本："仅用于模拟研究，不会自动交易"
- 系统本身没有真实下单代码路径

结论：**现有 API 已经是纯研究模式，无需禁用端点**

### 1.5 前端审查 ✅

已完成审查：
- ✅ `web/src/` - 54 个 TypeScript/TSX 文件
- ✅ 无 QMT、xtquant、真实下单相关代码
- ✅ `TradePlan` 是组合优化结果，不是真实订单
- ✅ UI 已有提示："仅用于模拟研究，不会自动下单"
- ✅ SystemSettingsPage 无交易/QMT 配置项

结论：**前端已经是纯研究模式，无需修改**

## Phase 2: 迁入 QMT 运行底座 ✅

已完成：
1. ✅ 迁入 `gateway/` Rust 模型 Gateway（纯模型代理，无交易逻辑）
2. ✅ 迁入 `tools/quote_bridge/` 行情桥（stdlib only，Tencent/Sina 数据源）
3. ✅ 迁入 `tools/news_bridge/` 新闻桥（stdlib only，Eastmoney 数据源）
4. ✅ 迁入服务控制脚本：
   - `scripts/service_control.ps1` - 统一服务生命周期管理
   - `scripts/llm_stack.ps1` - 本地模型服务
   - `scripts/start_stack.ps1` - 启动脚本
   - `scripts/stop_stack.ps1` - 停止脚本
   - `scripts/verify_stack.ps1` - 验证脚本
5. ✅ 更新 `.gitignore` 排除 `gateway/target/`
6. ✅ 验证 Gateway 编译（cargo check 通过，13.53s）
7. ✅ 验证 Bridge 服务（--help 正常，无额外依赖）

说明：
- Console 工具暂不迁入（包含"交易与风控"页面和实盘交易配置，需要大量清理才能符合研究只读模式）
- Gateway、Bridge 都是基础设施组件，无交易逻辑，可安全使用
- 服务控制脚本管理 Gateway、Bridge、Api、LocalModel 生命周期

## Phase 3: 统一 Compose、任务和生命周期 ⏳

待办：
- [ ] 扩展 `compose.yaml`
- [ ] 将编排任务包装为 Celery 任务
- [ ] 实现幂等、重试、checkpoint

## Phase 4-8: 后续阶段 ⏳

详见 `最终执行方案.md`

---

## 验收标准

### Phase 1 验收 ✅ COMPLETED

安全边界：
- [x] 配置层：无法通过环境变量启用 QMT/自动交易
- [x] 配置层：执行模式锁定为 RESEARCH_ONLY
- [x] Health：正确暴露研究只读状态
- [x] API 层：确认无真实交易端点（系统本身无下单代码）
- [x] 依赖：pyproject.toml 无 xtquant 依赖
- [x] 代码：全仓库无 xtquant/MiniQMT 导入
- [x] 前端：无交易/QMT 配置入口，已有模拟研究提示

**Phase 1 完成状态：研究只读闭锁已完成** ✅

关键发现：
- 系统架构本身就是纯研究系统，无真实下单路径
- TradePlan 是组合优化建议，不是真实订单执行
- 配置层 field_validator 确保无法通过任何途径启用交易
- 前端和后端都已明确标注"模拟研究""不会自动交易"

### 运行测试命令

```bash
# 配置和模型测试
export PYTHONPATH=src
.venv/Scripts/python.exe -m pytest tests/test_research_only_mode.py -v

# API 端点测试（待添加）
.venv/Scripts/python.exe -m pytest tests/test_trading_endpoints_disabled.py -v

# 完整测试套件
.venv/Scripts/python.exe -m pytest
```

### Phase 2 验收 ✅ COMPLETED

运行底座组件：
- [x] Gateway (Rust) 编译通过，可独立运行
- [x] Bridge 服务（市场桥、新闻桥）无额外依赖，可直接运行
- [x] 服务控制脚本已就位
- [x] Console 工具暂不迁入（包含交易界面，不符合研究只读要求）

**Phase 2 完成状态：QMT 运行底座（研究部分）已迁入** ✅

---

更新时间：2026-08-26
当前分支：main
**Phase 1 状态：✅ 完成**
**Phase 2 状态：✅ 完成**

下一步：Phase 3 - 统一 Compose、任务和生命周期
