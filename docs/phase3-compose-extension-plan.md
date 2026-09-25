# Phase 3: 统一 Compose、任务和生命周期

## 当前状态分析

### 现有 compose.yaml 架构

**核心服务：**
- `postgres` - PostgreSQL 数据库 (5432)
- `redis` - Redis 任务队列和缓存 (6379)
- `api` - FastAPI 应用 (8000)
- `web` - Nginx 前端服务 (80)

**Worker 拓扑：**
- `job-worker` (默认) - 串行处理所有队列，低内存占用 (700MB)
- `worker` (profile: parallel-workers) - 通用任务 worker
- `backtest-worker` (profile: parallel-workers) - 回测专用 worker
- `job-worker` - 统一研究任务 worker (scale: 1)
- `trade-plan-worker` (profile: parallel-workers) - 交易计划 worker
- `exit-advice-worker` - 退出建议 worker (320MB)

**可选服务：**
- `searxng` (profile: search) - 搜索引擎
- `edge-gateway` (profile: edge) - HTTPS 边缘网关

### 已迁入的 QMT 运行底座 (Phase 2)

**Rust Gateway：**
- 位置：`gateway/` (Rust/Cargo 项目)
- 功能：模型 Gateway，纯代理层，无交易逻辑
- 端口：8787 (可配置)
- 健康检查：`/health/ready`

**Python Bridge 服务：**
- `tools/quote_bridge/server.py` - 行情桥 (8081)
  - 数据源：Tencent qt.gtimg.cn / Sina hq.sinajs.cn
  - 健康检查：`/health`
- `tools/news_bridge/server.py` - 新闻桥 (8082)
  - 数据源：Eastmoney search API
  - 健康检查：`/health`

**PowerShell 控制脚本：**
- `scripts/service_control.ps1` - 统一生命周期管理
- `scripts/llm_stack.ps1` - 本地模型服务
- `scripts/start_stack.ps1` / `stop_stack.ps1` / `verify_stack.ps1`

### 现有任务系统

**Redis 队列架构：**
- 队列规范：`QueueSpec(kind, pending, processing, delayed?)`
- 活动队列：
  1. `personal-archive` - 个人归档任务
  2. `research` - 研究任务（由统一 `job-worker` 处理）
  3. `trade-plan` - 交易计划优化（模拟）
  4. `backtest` - 回测任务

**任务处理流程：**
1. `serial_worker.py` - 主控循环
   - 每秒轮询所有队列
   - 调用 `isolated_job.execute_isolated(kind, job_id)`
   - 子进程隔离执行，完成后回收内存
2. `isolated_job.py` - 子进程边界
   - 启动：`python -m ashare_ai.orchestration.run_job {kind} {job_id}`
3. `run_job.py` - 任务分发器
   - 根据 `kind` 加载对应 handler
   - 支持类型：schedule, research, trade-plan, backtest, exit-review, personal-archive, maintenance

**调度机制：**
- 定时任务：`runner.dispatch_scheduled_tasks()` (通过 `schedule` kind 触发)
- 维护任务：`maintenance_jobs.run_maintenance_job()` (每 300s)
- 能源节能模式：评估间隔 + 深度待机

## Phase 3 目标

### 3.1 将 Gateway 和 Bridge 服务加入 compose.yaml

**设计原则：**
- Gateway 和 Bridge 作为基础设施服务，与 postgres/redis 同级
- 默认启用（非 profile-gated），因为它们是数据层必需组件
- API 服务依赖 Gateway + Bridge 健康检查
- 使用 Docker 多阶段构建减少镜像体积

**Gateway 服务规范：**
```yaml
gateway:
  build:
    context: .
    dockerfile: docker/gateway.Dockerfile
    # 多阶段构建：builder stage (cargo build --release) + runtime stage (alpine)
  ports: ["${SERVICE_BIND_ADDRESS:-127.0.0.1}:8787:8787"]
  environment:
    RUST_LOG: info
    GATEWAY_BIND: "0.0.0.0:8787"
  restart: unless-stopped
  security_opt: ["no-new-privileges:true"]
  pids_limit: 64
  mem_limit: 128m
  logging: *bounded-logs
  healthcheck:
    test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://127.0.0.1:8787/health/ready"]
    interval: 30s
    timeout: 5s
    retries: 5
```

**Bridge 服务规范：**
```yaml
quote-bridge:
  build:
    context: .
    dockerfile: docker/bridge.Dockerfile
    target: quote-bridge
  ports: ["${SERVICE_BIND_ADDRESS:-127.0.0.1}:8081:8081"]
  command: ["python", "tools/quote_bridge/server.py", "--host", "0.0.0.0", "--port", "8081"]
  restart: unless-stopped
  security_opt: ["no-new-privileges:true"]
  pids_limit: 32
  mem_limit: 64m
  logging: *bounded-logs
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health')"]
    interval: 30s
    timeout: 5s
    retries: 5

news-bridge:
  build:
    context: .
    dockerfile: docker/bridge.Dockerfile
    target: news-bridge
  ports: ["${SERVICE_BIND_ADDRESS:-127.0.0.1}:8082:8082"]
  command: ["python", "tools/news_bridge/server.py", "--host", "0.0.0.0", "--port", "8082"]
  restart: unless-stopped
  security_opt: ["no-new-privileges:true"]
  pids_limit: 32
  mem_limit: 64m
  logging: *bounded-logs
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8082/health')"]
    interval: 30s
    timeout: 5s
    retries: 5
```

**API 依赖更新：**
```yaml
api:
  depends_on:
    # 现有依赖
    private-data-init:
      condition: service_completed_successfully
    postgres:
      condition: service_healthy
    redis:
      condition: service_healthy
    # 新增依赖
    gateway:
      condition: service_healthy
    quote-bridge:
      condition: service_healthy
    news-bridge:
      condition: service_healthy
```

### 3.2 创建 Dockerfile

**docker/gateway.Dockerfile:**
```dockerfile
# Builder stage
FROM rust:1.83-alpine AS builder
WORKDIR /build
RUN apk add --no-cache musl-dev
COPY gateway/Cargo.toml gateway/Cargo.lock ./
COPY gateway/src ./src/
RUN cargo build --release

# Runtime stage
FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /build/target/release/ashare-model-gateway /usr/local/bin/
EXPOSE 8787
CMD ["ashare-model-gateway"]
```

**docker/bridge.Dockerfile:**
```dockerfile
FROM python:3.12-alpine AS base
WORKDIR /app
RUN apk add --no-cache ca-certificates

# Quote bridge target
FROM base AS quote-bridge
COPY tools/quote_bridge/server.py /app/
EXPOSE 8081
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8081"]

# News bridge target
FROM base AS news-bridge
COPY tools/news_bridge/server.py /app/
EXPOSE 8082
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8082"]
```

### 3.3 任务系统无需修改

**原因：**
- 现有任务系统已经通过 Redis 队列解耦
- Gateway/Bridge 是被动服务，不产生任务
- API 层通过 HTTP 调用 Gateway/Bridge，无需队列
- 任务 handler 调用 API 内部方法，API 再调用 Gateway/Bridge

**数据流：**
```
用户请求 → API endpoint
         → 入队 Redis (enqueue_*)
         → serial_worker 轮询
         → isolated_job (子进程)
         → run_job.py 分发
         → 具体 handler (research_jobs/trade_plan_jobs/...)
         → 调用内部服务（scoring/backtest/market）
         → 内部服务调用 Gateway/Bridge (HTTP)
```

### 3.4 幂等性、重试、Checkpoint

**现有机制评估：**

**已实现：**
1. Redis Lease 机制 (`RedisLeasedQueue`)
   - 任务超时自动重入队列 (`requeue_expired()`)
   - Worker 心跳维持租约 (`heartbeat()`)
   - 显式确认完成 (`acknowledge()`)
2. 子进程隔离执行
   - 崩溃不影响主 worker
   - 内存自动回收
3. 数据库事务
   - 任务状态在数据库中持久化
   - 失败自动回滚

**待加强：**
1. **重试策略** - 当前失败任务不会自动重试
2. **幂等性标记** - 某些任务重复执行会产生副作用
3. **Checkpoint** - 长时间任务无中间状态保存

**增强方案：**

**3.4.1 为队列添加重试计数**

修改 `redis_queue.py` 支持：
- 任务元数据：`{job_id, retry_count, max_retries, created_at}`
- 失败后重入队列，`retry_count += 1`
- 达到 `max_retries` 后进入死信队列

**3.4.2 任务幂等性设计**

每个任务类型需要明确：
- `research` - 幂等（同样参数生成相同报告，数据库 unique constraint）
- `trade-plan` - 幂等（优化算法确定性，结果覆盖）
- `backtest` - 幂等（回测结果确定性）
- `personal-archive` - 幂等（归档操作可重复）
- `maintenance` - 幂等（清理操作可重复）

实现：
- 数据库 upsert 语义（ON CONFLICT DO UPDATE）
- 任务 ID 包含去重键（如 `report_id`）

**3.4.3 Checkpoint 机制**

对于长时间任务（backtest, research）：
- 在 Redis 中存储中间状态：`ashare:checkpoint:{job_id}` (hash)
- 定期更新进度：`{completed_symbols: [...], progress: 0.65}`
- 重启时检查 checkpoint，从断点恢复

## Phase 3 交付物

### 文件清单

**新增：**
- `docker/gateway.Dockerfile` - Gateway 镜像构建
- `docker/bridge.Dockerfile` - Bridge 多目标镜像构建
- `docs/phase3-compose-extension-plan.md` (本文件)

**修改：**
- `compose.yaml` - 添加 gateway, quote-bridge, news-bridge 服务
- `src/ashare_ai/orchestration/redis_queue.py` - 增强重试机制
- `src/ashare_ai/orchestration/backtest_jobs.py` - 添加 checkpoint
- `src/ashare_ai/orchestration/research_jobs.py` - 添加 checkpoint

**测试：**
- `tests/test_compose_services.py` - 验证新服务启动和健康检查
- `tests/test_retry_mechanism.py` - 验证任务重试逻辑

### 验收标准

**服务启动：**
- [ ] `docker compose up -d` 成功启动所有服务
- [ ] Gateway 健康检查通过 (http://127.0.0.1:8787/health/ready)
- [ ] Quote Bridge 健康检查通过 (http://127.0.0.1:8081/health)
- [ ] News Bridge 健康检查通过 (http://127.0.0.1:8082/health)
- [ ] API 依赖检查通过（等待所有依赖服务健康）

**任务系统：**
- [ ] 提交研究任务，serial_worker 正常处理
- [ ] 提交回测任务，成功调用 Gateway（模拟）
- [ ] 任务失败后自动重试（retry_count < max_retries）
- [ ] 达到最大重试次数后进入死信队列
- [ ] 长时间任务中断后从 checkpoint 恢复

**资源限制：**
- [ ] Gateway 内存占用 < 128MB
- [ ] Bridge 内存占用 < 64MB 每个
- [ ] 服务启动时间 < 10s

## 实施步骤

1. 创建 Dockerfile（gateway, bridge）
2. 扩展 compose.yaml（添加三个新服务）
3. 增强 redis_queue.py（重试机制）
4. 为 backtest/research 添加 checkpoint
5. 编写集成测试
6. 更新 `docs/fusion-progress.md` 标记 Phase 3 完成

---

更新时间：2026-08-26
状态：规划完成，待实施
