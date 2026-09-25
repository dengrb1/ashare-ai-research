# 单一 Job Worker 运行说明

研究专用分支固定使用 `SERIAL` 运行模式。Compose、GHCR 和 Windows/Linux 原生控制器都只启动一个
`job-worker`，它按租约顺序消费以下队列：研究、回测、Trade Plan、个人档案、退出建议、Jev 训练和
System-2 诊断。

## 启动

```bash
docker compose -p ashare-ai-src -f compose.yaml up -d --build
```

Worker 只保留轻量轮询父进程；重任务通过隔离子进程执行，完成后回收运行时内存。Redis 队列租约在
子进程异常退出时重新入队，数据库中的研究快照仍按 `symbol`、`trading_date`、`available_at` 和
`decision_at` 做 PIT 校验。

## 资源与节能

`LIGHTWEIGHT` 是默认运行档案。管理员开启节能模式后，Worker 在收盘且没有活动任务时进入深度待机，
保留调度和维护 tick；新任务或强制唤醒会在下一评估周期恢复消费。部署中只配置统一的
`job-worker`，不再拆分研究、回测或退出建议 Worker。

## 验证

- `tests/unit/test_serial_worker.py` 覆盖全部队列和租约轮询。
- `tests/unit/test_deployment_config.py` 确认 Compose 只有一个 `job-worker`。
- `tests/unit/test_energy_saving.py` 覆盖待机与恢复条件。
