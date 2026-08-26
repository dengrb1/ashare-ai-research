# Phase 4: 数据管道集成

## 目标

将 Gateway 和 Bridge 服务集成到现有数据管道中，使研究任务能够使用新迁入的基础设施组件获取行情、新闻和模型推理能力。

## 当前状态分析

### 已完成 (Phase 3)

- ✅ Gateway 服务运行在 8787 端口
- ✅ Quote Bridge 服务运行在 8081 端口
- ✅ News Bridge 服务运行在 8082 端口
- ✅ API 服务依赖这三个服务的健康检查
- ✅ Docker Compose 配置完成

### 现有数据管道

**市场数据层 (`src/ashare_ai/market/`)**:
- `data_source.py` - 数据源抽象
- `market_data.py` - 市场数据服务
- 当前可能使用：AKShare、Tushare 或其他公开数据源
- 需要确认是否已集成或需要集成 Quote Bridge

**特征工程 (`src/ashare_ai/features/`)**:
- 依赖市场数据
- 计算技术指标、因子等
- 可能需要实时行情数据

**AI Agent (`src/ashare_ai/agents/`)**:
- 需要调用模型 API
- 应该通过 Gateway 路由到 LLM 提供商
- 需要确认当前模型调用方式

**新闻分析**:
- 需要确认是否存在新闻分析模块
- News Bridge 提供 Eastmoney 新闻数据源
- 可能需要新增模块使用 News Bridge

## Phase 4 目标

### 4.1 集成 Quote Bridge 到市场数据层

**设计原则**:
- Quote Bridge 作为备用数据源，不替换现有数据源
- 通过配置开关控制是否启用
- 研究只读模式下可安全使用

**实施步骤**:

1. **审查现有市场数据实现**
   ```bash
   # 确认当前数据源
   grep -r "akshare\|tushare\|yfinance" src/ashare_ai/market/
   # 确认数据获取接口
   grep -r "def.*get.*quote\|def.*fetch.*price" src/ashare_ai/market/
   ```

2. **创建 Quote Bridge 客户端**
   - 文件：`src/ashare_ai/market/quote_bridge_client.py`
   - 功能：封装 HTTP 调用到 Quote Bridge
   - 接口：`get_quote(symbol: str) -> QuoteData`
   - 错误处理：网络超时、数据格式校验

3. **扩展数据源配置**
   - 在 `config.py` 添加：
     ```python
     quote_bridge_enabled: bool = Field(default=False)
     quote_bridge_url: str = Field(default="http://quote-bridge:8081")
     ```
   - 在 `market/data_source.py` 添加 QuoteBridge 数据源类型

4. **集成到市场数据服务**
   - 修改 `market/market_data.py`
   - 支持多数据源回退：Primary → QuoteBridge → Cached
   - 记录数据源使用指标

### 4.2 集成 News Bridge 到研究流程

**设计原则**:
- 新闻数据作为可选研究输入
- 不影响现有研究任务
- 可在研究任务中启用新闻分析

**实施步骤**:

1. **创建 News Bridge 客户端**
   - 文件：`src/ashare_ai/market/news_bridge_client.py`
   - 功能：封装 HTTP 调用到 News Bridge
   - 接口：`get_news(query: str) -> List[NewsItem]`

2. **扩展研究任务参数**
   - 在研究任务 schema 中添加：
     ```python
     include_news_analysis: bool = False
     ```

3. **新增新闻分析模块**
   - 文件：`src/ashare_ai/features/news_features.py`
   - 功能：从新闻提取情感、主题、关键词
   - 依赖：News Bridge Client + 可选 AI Agent

4. **集成到研究 handler**
   - 修改 `orchestration/research_jobs.py`
   - 当 `include_news_analysis=True` 时调用新闻模块
   - 将新闻特征加入研究报告

### 4.3 集成 Gateway 到 AI Agent

**设计原则**:
- Gateway 作为统一模型入口
- 支持多模型切换（OpenAI, Claude, 本地模型）
- 保持 Agent 代码与模型提供商解耦

**实施步骤**:

1. **审查现有 AI Agent 实现**
   ```bash
   # 确认当前模型调用方式
   grep -r "openai\|anthropic\|llm" src/ashare_ai/agents/
   # 确认 Agent 使用场景
   grep -r "agent" src/ashare_ai/orchestration/
   ```

2. **创建 Gateway 客户端**
   - 文件：`src/ashare_ai/agents/gateway_client.py`
   - 功能：OpenAI 兼容接口调用 Gateway
   - 接口：
     ```python
     def chat_completion(
         model: str,
         messages: List[dict],
         temperature: float = 0.7,
         **kwargs
     ) -> dict
     ```

3. **配置 Gateway 连接**
   - 在 `config.py` 添加：
     ```python
     gateway_enabled: bool = Field(default=False)
     gateway_url: str = Field(default="http://gateway:8787")
     gateway_api_key: str = Field(default="")
     ```

4. **重构 Agent 模块使用 Gateway**
   - 修改 `agents/` 下所有直接调用 OpenAI/Anthropic 的代码
   - 通过配置开关选择 Gateway 或直连
   - 保持现有 Agent 接口不变

### 4.4 健康检查和监控

**实施步骤**:

1. **扩展健康检查端点**
   - 修改 `api/app.py` 的 `/api/v1/health` 端点
   - 添加字段：
     ```python
     quote_bridge_status: str  # "ok" | "unavailable"
     news_bridge_status: str   # "ok" | "unavailable"
     gateway_status: str       # "ok" | "unavailable"
     ```

2. **内部健康探测**
   - 文件：`src/ashare_ai/core/health.py`
   - 功能：
     ```python
     async def check_quote_bridge() -> bool
     async def check_news_bridge() -> bool
     async def check_gateway() -> bool
     ```
   - 实现：简单 HTTP GET /health，超时 2 秒

3. **服务降级策略**
   - Quote Bridge 不可用 → 使用主数据源
   - News Bridge 不可用 → 跳过新闻分析
   - Gateway 不可用 → 使用直连模型或报错

## 数据流设计

### 研究任务数据流（扩展后）

```
用户提交研究任务
  ↓
API enqueue (Redis)
  ↓
serial_worker 获取任务
  ↓
isolated_job (子进程)
  ↓
research_jobs.execute_research_job()
  ↓
├─ 市场数据获取
│   ├─ 主数据源 (AKShare/Tushare)
│   └─ [可选] Quote Bridge (实时行情补充)
│
├─ [可选] 新闻数据获取
│   └─ News Bridge → 新闻分析 → 情感特征
│
├─ 特征工程
│   ├─ 技术指标
│   ├─ 因子计算
│   └─ [可选] 新闻特征
│
├─ [可选] AI Agent 分析
│   └─ Gateway → LLM → 分析报告
│
└─ 生成研究报告
    └─ 存储到数据库
```

### 回测任务数据流（可能扩展）

```
回测任务
  ↓
backtest_jobs.execute_backtest()
  ↓
历史数据获取
  ├─ 主数据源
  └─ [可选] Quote Bridge (历史行情验证)
  ↓
回测引擎运行
  ↓
结果存储
```

## 配置变更

### config.py 新增字段

```python
class Settings(BaseSettings):
    # ... 现有字段 ...
    
    # Phase 4: Data pipeline integration
    quote_bridge_enabled: bool = Field(
        default=False,
        description="Enable Quote Bridge as supplementary market data source"
    )
    quote_bridge_url: str = Field(
        default="http://quote-bridge:8081",
        description="Quote Bridge service URL"
    )
    
    news_bridge_enabled: bool = Field(
        default=False,
        description="Enable News Bridge for news analysis in research tasks"
    )
    news_bridge_url: str = Field(
        default="http://news-bridge:8082",
        description="News Bridge service URL"
    )
    
    gateway_enabled: bool = Field(
        default=False,
        description="Use Gateway as unified model API entry point"
    )
    gateway_url: str = Field(
        default="http://gateway:8787",
        description="Model Gateway service URL"
    )
    gateway_api_key: str = Field(
        default="",
        description="API key for Gateway authentication"
    )
```

## 交付物清单

### 新增文件

- `src/ashare_ai/market/quote_bridge_client.py` - Quote Bridge HTTP 客户端
- `src/ashare_ai/market/news_bridge_client.py` - News Bridge HTTP 客户端
- `src/ashare_ai/agents/gateway_client.py` - Gateway HTTP 客户端
- `src/ashare_ai/features/news_features.py` - 新闻特征提取模块
- `src/ashare_ai/core/health.py` - 基础设施健康检查
- `tests/test_quote_bridge_integration.py` - Quote Bridge 集成测试
- `tests/test_news_bridge_integration.py` - News Bridge 集成测试
- `tests/test_gateway_integration.py` - Gateway 集成测试
- `docs/phase4-data-pipeline-plan.md` (本文件)

### 修改文件

- `src/ashare_ai/core/config.py` - 添加 Bridge/Gateway 配置字段
- `src/ashare_ai/api/app.py` - 扩展健康检查端点
- `src/ashare_ai/market/data_source.py` - 添加 QuoteBridge 数据源类型
- `src/ashare_ai/market/market_data.py` - 集成 Quote Bridge 调用
- `src/ashare_ai/orchestration/research_jobs.py` - 集成新闻分析
- `src/ashare_ai/agents/*.py` - 重构为使用 Gateway（如果使用 Agent）

### 文档

- `docs/fusion-progress.md` - 标记 Phase 4 完成
- `docs/API.md` - 更新健康检查端点文档（如果存在）

## 验收标准

### 功能验收

- [ ] Quote Bridge 客户端可以成功获取行情数据
- [ ] News Bridge 客户端可以成功获取新闻数据
- [ ] Gateway 客户端可以成功调用模型 API
- [ ] 健康检查端点正确反映三个服务的状态
- [ ] 研究任务可以选择性启用新闻分析
- [ ] 市场数据服务在 Quote Bridge 不可用时能降级到主数据源

### 安全验收

- [ ] 所有客户端实现超时控制（防止阻塞）
- [ ] 所有客户端实现异常处理（服务不可用不影响主流程）
- [ ] Gateway API Key 从环境变量读取，不硬编码
- [ ] 配置默认值保持向后兼容（新功能默认关闭）

### 测试验收

- [ ] 单元测试覆盖所有客户端（mock HTTP 响应）
- [ ] 集成测试验证端到端数据流
- [ ] 服务降级场景测试通过
- [ ] 健康检查测试通过

### 性能验收

- [ ] Quote Bridge 调用延迟 < 500ms (P95)
- [ ] News Bridge 调用延迟 < 2s (P95)
- [ ] Gateway 调用延迟取决于模型，但客户端超时保护 < 30s
- [ ] 服务不可用时降级路径无明显延迟

## 实施步骤顺序

1. **审查现有代码**（1-2 小时）
   - 确认市场数据当前实现
   - 确认 AI Agent 当前实现
   - 确认是否已有新闻分析模块

2. **创建客户端库**（2-3 小时）
   - QuoteBridgeClient
   - NewsBridgeClient  
   - GatewayClient
   - 健康检查工具

3. **集成到数据层**（2-3 小时）
   - 修改 market_data.py
   - 修改 config.py
   - 修改健康检查端点

4. **集成到研究流程**（2-4 小时）
   - 新增 news_features.py
   - 修改 research_jobs.py
   - 重构 agents/ (如果需要)

5. **编写测试**（2-3 小时）
   - 单元测试
   - 集成测试
   - 降级场景测试

6. **文档更新**（30 分钟）
   - 更新 fusion-progress.md
   - 更新 API.md (如果存在)

**预计总时间**: 10-15 小时

## 风险和注意事项

### 技术风险

1. **现有代码依赖冲突**
   - 风险：现有市场数据模块可能与 Quote Bridge 接口不兼容
   - 缓解：先审查代码，设计适配器模式

2. **服务不稳定**
   - 风险：Bridge 服务可能不稳定（Python stdlib HTTP 服务）
   - 缓解：实现降级策略，主数据源为主，Bridge 为辅

3. **数据格式差异**
   - 风险：Quote Bridge 返回格式可能与现有数据源不同
   - 缓解：客户端层做数据标准化

### 业务风险

1. **性能下降**
   - 风险：新增网络调用可能拖慢研究任务
   - 缓解：并行调用、超时控制、可选启用

2. **数据质量**
   - 风险：Bridge 数据质量可能不如主数据源
   - 缓解：仅作为补充数据源，不替换主源

## 后续阶段预览

完成 Phase 4 后，系统将具备：
- ✅ 研究只读模式安全边界 (Phase 1)
- ✅ QMT 基础设施组件 (Phase 2)
- ✅ Docker Compose 统一编排 (Phase 3)
- ✅ 数据管道集成 (Phase 4)

下一步 (Phase 5-8):
- Phase 5: 模型训练集成（如果需要）
- Phase 6: Rust 优化（如果需要）
- Phase 7: Web/PWA 增强（如果需要）
- Phase 8: 离线部署包（如果需要）

---

更新时间：2026-08-26
状态：规划完成，待实施
