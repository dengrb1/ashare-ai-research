# 分支更新：双研究任务并发

分支：`codex/read-codex-goal-objective`

## 背景

默认 `job-worker` 完全串行消费研究、交易方案、卖出建议和回测任务；即使启用 `parallel-workers` profile，原先也只启动一个 `research-worker`。  
研究队列与运行产物本身已按 `run_id` 隔离，因此可以在不改研究语义的前提下，用两个研究 Worker 同时推进不同研究任务。

## 本次变更

### 1. Compose

- 文件：`compose.yaml`
- 为 `research-worker` 增加 `scale: 2`
- 仍仅在 `parallel-workers` profile 启用
- 每个副本保持：
  - 串行消费一条研究任务
  - 内存限制 `700m`
  - 既有 Redis 租约机制

默认低内存部署不变：未启用 profile 时仍由单个 `job-worker` 串行处理。

### 2. 文档

- `docs/DOCKER_DEPLOY.md`
  - 高配启动命令固定为：

```bash
docker compose -p ashare-ai-src -f compose.yaml --profile parallel-workers \
  up -d --build --scale job-worker=0 --scale research-worker=2
```

  - 明确禁止默认 `job-worker` 与专用 Worker 同时消费研究队列
  - 注明并发模式至少需要 4GB 可用内存
  - 说明两条研究各自最多 4 路 LLM 组件请求，网关需支持最多 8 路；容量不足时可将 `LLM_AGENT_MAX_CONCURRENCY` 降为 `2`，任务级双并发不变

- `README.md`
  - 同步高配并行研究 Worker 启动说明与资源约束

### 3. 测试

- `tests/unit/test_deployment_config.py`
  - 断言 `research-worker.scale == 2`
  - 断言仍属于 `parallel-workers` profile
  - 断言默认 `job-worker` 未设置多副本 `scale`

- `tests/unit/test_redis_leased_queue.py`
  - 两个独立消费者从同一研究队列领取不同 `run_id`
  - 断言互不重复，第三次领取为空

## 明确未改动

- `/api/v1/research/runs` 提交接口
- `active_research_key` 去重逻辑
- 数据库模型与 Alembic 迁移
- 研究内部评分、PIT、审计、取消、数据就绪重试、队列租约语义

相同请求仍复用活动研究；不同研究任务可同时运行两条，额外任务继续在 Redis 队列等待。

## 使用方式

### 默认低内存

```bash
docker compose -p ashare-ai-src -f compose.yaml up -d --build
```

### 高配双研究并发

```bash
docker compose -p ashare-ai-src -f compose.yaml --profile parallel-workers \
  up -d --build --scale job-worker=0 --scale research-worker=2
```

切换前确认没有正在运行的研究或回测任务。不要让默认 `job-worker` 与 `research-worker` 同时消费研究队列。

## 验收要点

- 受影响单元测试通过
- 默认栈：API / Web / PostgreSQL / Redis / job-worker 健康
- 并行栈：`job-worker` 为 0，`research-worker` 有 2 个运行副本
- 并行栈：API / Web / PostgreSQL / Redis 与两个研究 Worker 健康

## 假设

- “两个任务”指两个不同的研究运行（不同 `run_id`）
- 不改变研究任务内部业务规则，只提高任务级吞吐
