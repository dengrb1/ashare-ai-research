# 节能模式（Energy Saving Mode）

收盘后、当日每日研究全部完成之后进入的低活动状态。默认关闭；开启后只保留基本功能（API、Web、
PostgreSQL、Redis、定时器），其余服务进入深度待机或由宿主控制器停止，并预留接口随时恢复。

## 它能省多少内存

以 2026-08-06 空闲基线（`docs/performance/resource-baseline-20260806.md`）为参考，真正可以被
"关闭"释放的是三个非基本服务：

| 服务 | cgroup 工作集 | 匿名内存 | 节能模式下 |
|---|---|---|---|
| searxng | ~117 MiB | ~104 MiB | 可停止（仅研究检索使用） |
| job-worker | ~311 MiB | ~60 MiB | 可停止（收盘后无队列任务） |
| exit-advice-worker | ~48 MiB | ~33 MiB | 可停止（按需触发） |
| **合计** | **~476 MiB** | **~197 MiB** | 停掉即释放 |

必须保留：`api`（~137 MiB，Web/API 界面）、`postgres`（~46 MiB）、`redis`（~17 MiB）、
`web`（~27 MiB）。

两层机制，缺一不可：

1. **应用层（本次已实现）**：决定"何时安全"——收盘 + 无任何进行中的研究/回测/Trade Plan/退出建议；
   让 Worker 进入深度待机（停止逐秒轮询、心跳标记 `energy_saving`），并把期望状态暴露给宿主。
2. **宿主层（由拓扑控制器/运维执行）**：真正停掉容器。API 按安全设计不访问 Docker socket，只通过
   `/api/internal/topology-desired` 暴露 `energy_saving_active` 信号；宿主控制器据此执行：

```bash
# 进入节能（由控制器在 energy_saving_active=true 时执行）
docker compose -p ashare-ai-src -f compose.yaml stop searxng job-worker exit-advice-worker

# 恢复（energy_saving_active=false 或管理员强制唤醒后）
docker compose -p ashare-ai-src -f compose.yaml up -d searxng job-worker exit-advice-worker
```

没有宿主控制器的部署，应用层仍能减少 Worker 轮询 CPU，并让 searxng 等容器保持原样（此时内存
节省主要来自容器停止，需要人工或外部 cron 执行上述命令）。

> 说明：API 进程本身（~137 MiB）不会被本模式显著降低——Python 运行时不能卸载已导入模块；API
> 的行情子进程与缓存已在收盘后由 `api_runtime_auto_close` 机制自动回收，本模式不重复实现。

## 如何开启

- 环境变量：`.env` 中 `ENERGY_SAVING_ENABLED=true`。
- 或系统设置中心：`PUT /api/v1/admin/system-settings`，字段 `energy_saving_enabled`（热加载，
  无需重启 Worker；Worker 每个评估周期重新解析）。

## 进入与退出条件

进入（全部满足）：`energy_saving_enabled=true` 且已收盘（`is_after_close`，含周末）且没有任何
`PENDING/QUEUED/RUNNING/PROCESSING/DATA_READINESS_WAITING/CANCEL_REQUESTED` 的研究、回测、
Trade Plan 或退出建议任务。

退出（任一满足，下个评估周期 ≤60 秒内 Worker 恢复）：

- 任何新任务入队（对应 DB 行进入活动状态）；
- 下一次定时研究派发（`15:05` 调度 tick 创建研究任务）；
- 管理员 `POST /api/v1/admin/energy-saving/disable`（强制唤醒，标记 12 小时过期）；
- 重新收盘前的任意时刻（`is_after_close` 变为假）。

## 接口

| 接口 | 权限 | 作用 |
|---|---|---|
| `GET /api/v1/admin/energy-saving` | 管理员 | 返回 `enabled/active/reason/manual_wake/entered_at/updated_at/deep_standby_seconds` |
| `POST /api/v1/admin/energy-saving/disable` | 管理员 | 强制唤醒一个周期（不修改配置，TTL 12 小时） |
| `POST /api/v1/admin/energy-saving/enable` | 管理员 | 重新启用自动进入（清除唤醒标记） |
| `GET /api/internal/topology-desired` | `TOPOLOGY_CONTROLLER_TOKEN` | 额外返回 `energy_saving_active/since/reason/enabled` 供宿主控制器停/启服务 |

`enable`/`disable` 是带 TTL、可逆的操作状态，不需要系统设置解锁。持久化开关是
`energy_saving_enabled` 系统设置（默认拒绝）。

## 边界与不变量

- 不影响 PIT、Manifest、确定性评分、交易规则、回测复现与审计；不改变任何领域输出或哈希。
- 深度待机期间 `job-worker` 仍保留 maintenance 与 schedule 定时 tick（隔离子进程，短生命周期），
  因此次日研究派发不会丢失。
- 任何评估异常（Redis/DB 不可用）都让 Worker 保持清醒轮询，绝不让节能 bug 阻塞真实工作。
- `enable`/`disable` 不改变已冻结的研究或快照；停止容器不删除任何数据卷。
- DUAL 拓扑的 `research-worker` 本版未纳入深度待机（默认 SERIAL 拓扑覆盖）；如需在 DUAL 下节能，
  由宿主控制器直接停止 `research-worker` 容器即可。

## 验证

- 单测：`tests/unit/test_energy_saving.py`（进入/退出/强制唤醒/Redis 故障降级）、
  `tests/unit/test_serial_worker.py`、`tests/unit/test_exit_advice_worker.py`（深度待机不轮询、
  仍跑调度）、`tests/unit/test_api_audit.py`（管理接口）。
- 端到端记忆节省需要真实收盘后负载（授权后）：在节能前后各采样一次 cgroup 工作集，对比
  `searxng/job-worker/exit-advice-worker` 三容器是否已停止。
