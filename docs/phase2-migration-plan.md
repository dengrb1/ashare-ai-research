# Phase 2: QMT 基础设施组件迁移计划

## 目标

将 QMT 仓库中支持研究功能的基础设施组件迁移到 ashare-ai-src，同时确保：
1. 不引入任何交易执行能力
2. 保持研究只读模式的安全边界
3. 组件在无 xtquant 环境下可正常工作

## 迁移组件清单

### 1. Model Gateway (gateway/)

**来源**: `F:\code\qmt\gateway\`
**目标**: `F:\code\ashare-ai-src\gateway\`

**组件内容**:
- Rust 实现的 OpenAI 协议兼容模型网关
- 多渠道路由、模型映射、Key 池管理
- 用于 AI Agent 后端，不涉及交易

**迁移动作**:
- [x] 确认组件用途（✅ 纯 AI 基础设施）
- [ ] 直接复制整个 gateway/ 目录
- [ ] 验证 Cargo.toml 依赖无交易相关库
- [ ] 检查 config.toml 配置项
- [ ] 更新 .gitignore 包含 gateway/target/

### 2. Console Tool (tools/console/)

**来源**: `F:\code\qmt\tools\console\`
**目标**: `F:\code\ashare-ai-src\tools\console\`

**组件内容**:
- Windows 桌面配置工具（Tkinter）
- 服务管理、健康检查、配置编辑
- 包含 gateway、bridge 等基础设施管理

**迁移动作**:
- [x] 确认组件用途（✅ 基础设施管理）
- [ ] 复制 console_app.py、config_store.py、stack_controller.py
- [ ] 审查代码，移除任何 QMT/交易相关配置项
- [ ] 复制 build.ps1 和 .spec 文件
- [ ] 验证无 xtquant 导入

### 3. Service Control Scripts (scripts/)

**来源**: `F:\code\qmt\scripts\`
**目标**: `F:\code\ashare-ai-src\scripts\` (需要重命名避免冲突)

**组件内容**:
- service_control.ps1 - 后台服务生命周期管理
- llm_stack.ps1 - 本地模型服务
- verify_stack.ps1 - 栈验证
- self_check.ps1 - 自检

**迁移动作**:
- [ ] 创建 scripts/qmt/ 子目录（避免与现有 topology controller 冲突）
- [ ] 复制 service_control.ps1
- [ ] 审查服务定义，移除交易相关服务（如果有）
- [ ] 保留: Gateway, QuoteBridge, NewsBridge, LocalModel
- [ ] 移除: 任何订单执行、QMT 连接相关服务
- [ ] 复制 llm_stack.ps1、verify_stack.ps1、self_check.ps1
- [ ] 更新路径引用适配新仓库结构

### 4. Quote Bridge (tools/quote_bridge/)

**来源**: `F:\code\qmt\tools\quote_bridge\`
**目标**: `F:\code\ashare-ai-src\tools\quote_bridge\`

**组件内容**:
- 行情数据桥接服务
- 用于研究数据获取

**迁移动作**:
- [ ] 检查目录内容
- [ ] 确认无交易下单能力
- [ ] 直接复制

### 5. News Bridge (tools/news_bridge/)

**来源**: `F:\code\qmt\tools\news_bridge\`
**目标**: `F:\code\ashare-ai-src\tools\news_bridge\`

**组件内容**:
- 新闻数据桥接服务
- 用于研究信息获取

**迁移动作**:
- [ ] 检查目录内容
- [ ] 确认无交易相关功能
- [ ] 直接复制

## 不迁移的组件

以下 QMT 组件**不迁移**，因为与交易执行或 QMT 连接相关：

- [ ] 任何 xtquant 集成代码
- [ ] 订单执行模块
- [ ] QMT 账户连接逻辑
- [ ] 自动交易策略执行器

## 验收标准

### 功能验收
- [ ] Gateway 可独立编译运行
- [ ] Console 工具可启动并管理服务
- [ ] service_control.ps1 可启动/停止基础服务
- [ ] llm_stack.ps1 可管理本地模型
- [ ] Quote/News Bridge 可正常获取数据

### 安全验收
- [ ] 所有迁移组件无 xtquant 导入
- [ ] 所有迁移组件无订单提交代码
- [ ] Console 配置界面无交易/QMT 设置项
- [ ] service_control.ps1 服务列表无交易执行服务

### 集成验收
- [ ] compose.yaml 可选包含这些组件（Profile）
- [ ] API 服务可通过 Gateway 连接模型
- [ ] 研究流程可使用 Quote/News Bridge 数据

## 执行顺序

1. **Gateway** (最独立，无依赖)
2. **Bridge 服务** (数据层，被其他组件依赖)
3. **Scripts** (管理层，依赖服务定义)
4. **Console** (UI 层，依赖脚本和服务)

## 注意事项

1. **路径适配**: QMT 脚本硬编码了很多路径，需要更新为 ashare-ai-src 结构
2. **配置文件**: config/settings.json 格式可能不同，需要映射
3. **端口冲突**: 确保端口配置与现有 compose.yaml 不冲突
4. **依赖隔离**: 这些组件应该可选启动，不影响核心 API 服务

---

更新时间：2026-08-26
状态：规划完成，开始执行
