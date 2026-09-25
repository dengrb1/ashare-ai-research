# 节能模式

节能模式在收盘、当日研究完成且没有活动任务时，让单一 `job-worker` 进入深度待机。API、Web、
PostgreSQL 和 Redis 保持运行；Worker 仍执行维护和调度 tick，避免错过下一交易日研究派发。

## 条件

进入条件全部满足：`energy_saving_enabled=true`、已收盘、没有研究/回测/Trade Plan/退出建议或
个人档案任务处于活动状态。新任务入队、定时研究派发或管理员强制唤醒后，Worker 在下一评估周期恢复。

## 管理接口

| 接口 | 作用 |
|---|---|
| `GET /api/v1/admin/energy-saving` | 返回配置、当前状态和进入原因 |
| `POST /api/v1/admin/energy-saving/enable` | 清除临时唤醒标记，恢复自动进入 |
| `POST /api/v1/admin/energy-saving/disable` | 强制唤醒一个周期 |
| `GET /api/internal/topology-desired` | 返回宿主控制器需要的节能信号 |

宿主控制器可以根据 `energy_saving_active` 停止或恢复 `job-worker`，API 不访问 Docker socket：

```bash
docker compose -p ashare-ai-src -f compose.yaml stop job-worker
docker compose -p ashare-ai-src -f compose.yaml up -d job-worker
```

## 不变量

- 不改变 PIT、冻结快照、评分、回测、审计或队列租约语义。
- Worker 任务在隔离子进程中运行，完成后执行运行时内存回收。
- Redis/数据库评估失败时保持清醒轮询，节能故障不会阻塞研究任务。

## 验证

`tests/unit/test_energy_saving.py` 覆盖进入、退出、强制唤醒和 Redis 故障降级；
`tests/unit/test_serial_worker.py` 覆盖待机期间的调度维护行为。
