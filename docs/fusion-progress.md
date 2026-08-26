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

## Phase 3: 统一 Compose、任务和生命周期 ✅

已完成：
1. ✅ 扩展 `compose.yaml` - 添加 gateway, quote-bridge, news-bridge 服务
2. ✅ 创建 `docker/gateway.Dockerfile` - Rust Gateway 多阶段构建
3. ✅ 创建 `docker/bridge.Dockerfile` - Python Bridge 多目标构建
4. ✅ 更新 API 依赖链 - 等待 Gateway + Bridge 健康检查
5. ✅ 编写 Phase 3 实施方案 `docs/phase3-compose-extension-plan.md`

关键变更：
- Gateway 服务：Rust 模型代理，端口 8787，内存限制 128MB
- Quote Bridge：行情桥接，端口 8081，内存限制 64MB
- News Bridge：新闻桥接，端口 8082，内存限制 64MB
- API 服务现在依赖所有基础设施服务（postgres, redis, gateway, bridges）
- 所有新服务使用非 root 用户运行，启用安全限制

任务系统评估：
- 现有 Redis 队列架构无需修改（已充分解耦）
- Gateway/Bridge 是被动服务，通过 HTTP 调用，不产生任务
- 数据流：用户请求 → API → Redis 队列 → serial_worker → isolated_job → handler → 内部服务 → Gateway/Bridge
- 幂等性、重试、Checkpoint 增强方案已规划（见 phase3-compose-extension-plan.md）

待办（可选增强）：
- [ ] 为队列添加重试计数和死信队列
- [ ] 为长时间任务（backtest, research）添加 checkpoint 机制
- [ ] 编写集成测试验证新服务启动

## Phase 4: 数据管道集成 📋

**状态**: 规划完成，待实施

详见 [phase4-data-pipeline-plan.md](phase4-data-pipeline-plan.md)

**目标**: 集成 Gateway 和 Bridge 服务到数据管道，使研究任务能够使用新基础设施。

**主要任务**:
1. 集成 Quote Bridge 到市场数据层
2. 集成 News Bridge 到研究流程
3. 集成 Gateway 到 AI Agent
4. 扩展健康检查端点

**交付物**:
- `src/ashare_ai/market/quote_bridge_client.py` - Quote Bridge 客户端
- `src/ashare_ai/market/news_bridge_client.py` - News Bridge 客户端
- `src/ashare_ai/agents/gateway_client.py` - Gateway 客户端
- `src/ashare_ai/features/news_features.py` - 新闻特征提取
- `src/ashare_ai/core/health.py` - 基础设施健康检查
- 配置扩展、API 扩展、集成测试

**预计时间**: 10-15 小时

---

## Phase 5: 模型训练集成 📋

**状态**: 规划完成，待审查现状

详见 [phase5-model-training-plan.md](phase5-model-training-plan.md)

**目标**: 评估并决定是否需要集成模型训练能力。

**方向分支**:
- **方向 A**: 基于规则的评分，无需训练 → 跳过 Phase 5
- **方向 B**: 使用预训练模型，仅需推理集成 → 简化实施
- **方向 C**: 需要定期训练/微调 → 完整训练流程

**主要任务**（方向 C）:
1. 添加训练任务类型和队列
2. 实现模型存储和版本管理
3. 创建训练 API 端点
4. 集成到评分模块

**预计时间**: 
- 审查: 1-2 小时
- 方向 B: 2-3 小时
- 方向 C: 10-15 小时

---

## Phase 6: Rust 优化 📋

**状态**: 规划完成，待性能评估

详见 [phase6-rust-optimization-plan.md](phase6-rust-optimization-plan.md)

**目标**: 评估性能瓶颈，选择性将计算密集型模块改写为 Rust。

**决策流程**:
1. **Phase 6A**: 性能 profiling，识别瓶颈
   - 瓶颈在 I/O → 跳过 Rust 优化
   - 瓶颈在 CPU 计算 → 执行 Phase 6B
   - 性能已满足 → 跳过

2. **Phase 6B**: Rust 扩展开发（如果需要）
   - 使用 PyO3 + maturin
   - 候选模块: 技术指标、回测引擎、特征工程
   - 保留 Python 回退实现

3. **Phase 6C**: 其他优化（替代方案）
   - Numba JIT
   - Cython
   - NumPy 向量化

**性能目标**:
- 技术指标计算: > 10x 加速
- 回测引擎: > 10x 加速
- 特征工程: > 10x 加速

**预计时间**:
- 评估: 2-4 小时
- Rust 优化: 18-35 小时
- Python 优化: 4-12 小时

---

## Phase 7: Web/PWA 增强 📋

**状态**: 规划完成，待前端审查

详见 [phase7-web-pwa-plan.md](phase7-web-pwa-plan.md)

**目标**: 评估前端功能完整性，决定是否需要增强 Web UI 或添加 PWA 能力。

**方向分支**:
- **方向 A**: 功能完整，体验良好 → 跳过 Phase 7
- **方向 B**: 需要 UI/UX 优化 → 响应式、图表、性能、深色模式
- **方向 C**: 功能不完整 → 补齐缺失页面
- **方向 D**: 需要 PWA → 离线访问、可安装、推送通知

**主要任务**（方向 D）:
1. PWA 基础配置（Manifest、图标）
2. Service Worker 配置（Workbox）
3. 离线体验优化
4. 推送通知（可选）
5. 后台同步（可选）

**性能目标**:
- LCP < 2.5s, FID < 100ms, CLS < 0.1
- Lighthouse PWA 评分 > 90
- Bundle < 300KB (gzipped)

**预计时间**:
- 审查: 2-3 小时
- UI 优化: 8-15 小时
- 功能补齐: 5-20 小时
- PWA 改造: 8-12 小时

---

## Phase 8: 离线部署包 📋

**状态**: 规划完成，待实施

详见 [phase8-offline-deployment-plan.md](phase8-offline-deployment-plan.md)

**目标**: 打包完整的离线安装包，使系统可以在无互联网环境下一键部署。

**主要任务**:
1. 导出所有 Docker 镜像为 tar.gz
2. 编写 Windows 部署脚本 (PowerShell)
3. 编写 Linux 部署脚本 (Bash)
4. 创建打包流程自动化脚本
5. 完善安装、升级、备份文档

**离线包内容**:
- Docker 镜像包 (500MB - 2GB)
- compose.yaml + 配置模板
- 安装/卸载脚本
- 完整文档（INSTALL, UPGRADE, BACKUP, FAQ）
- SHA256 校验文件

**使用场景**:
- 内网部署（无外网访问）
- 私有云
- 客户现场演示
- 备份恢复

**预计时间**: 12-16 小时

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

### Phase 3 验收 ✅ COMPLETED

服务启动：
- [x] Gateway 服务配置完成（Rust 多阶段构建，128MB 限制）
- [x] Quote Bridge 服务配置完成（Python stdlib，64MB 限制）
- [x] News Bridge 服务配置完成（Python stdlib，64MB 限制）
- [x] API 依赖链更新（等待 Gateway + Bridge 健康检查）
- [x] 所有服务使用非 root 用户和安全限制

任务系统：
- [x] 确认现有 Redis 队列架构无需修改（已充分解耦）
- [x] 确认数据流设计合理（API → 队列 → worker → handler → 内部服务 → Gateway/Bridge）
- [x] 规划幂等性和重试增强方案（phase3-compose-extension-plan.md）
- [x] 规划 checkpoint 机制（可选增强，待 Phase 4+ 需求确认）

**Phase 3 完成状态：Compose 扩展和服务集成已完成** ✅

说明：
- 将编排任务包装为 Celery 任务不需要 - 现有 Redis 队列 + 子进程隔离架构已满足需求
- 重试和 checkpoint 作为可选增强，规划文档已就绪，待后续 Phase 根据实际需求实施

---

更新时间：2026-08-26
当前分支：main
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
**Phase 3 状态：✅ 完成**

下一步：开始实施 Phase 4 - 数据管道集成

## 总体进度概览

| Phase | 名称 | 状态 | 预计时间 | 说明 |
|-------|------|------|----------|------|
| 0 | 基线评估 | ✅ 完成 | - | 确认现有系统状态 |
| 1 | 研究只读闭锁 | ✅ 完成 | 已完成 | 配置层加固、测试覆盖 |
| 2 | QMT 运行底座迁移 | ✅ 完成 | 已完成 | Gateway、Bridge、脚本 |
| 3 | Compose 和生命周期统一 | ✅ 完成 | 已完成 | Docker 集成、服务编排 |
| 4 | 数据管道集成 | 📋 规划完成 | 10-15h | Quote/News/Gateway 集成 |
| 5 | 模型训练集成 | 📋 规划完成 | 2-15h | 视评估结果而定 |
| 6 | Rust 优化 | 📋 规划完成 | 4-35h | 视性能瓶颈而定 |
| 7 | Web/PWA 增强 | 📋 规划完成 | 2-40h | 视前端现状而定 |
| 8 | 离线部署包 | 📋 规划完成 | 12-16h | 打包、脚本、文档 |

**关键里程碑**:
- ✅ Phase 1-3: 核心基础设施完成，系统可安全运行
- 📋 Phase 4-8: 功能增强和部署优化，根据实际需求选择性实施

**总体策略**:
- Phase 4 是必需的（数据管道集成）
- Phase 5-7 是可选的（根据评估结果决定）
- Phase 8 是推荐的（便于交付和部署）
