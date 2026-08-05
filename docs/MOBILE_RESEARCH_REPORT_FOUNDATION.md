# 移动端研究报告基础契约

本文定义 Android/iOS 研究报告页在 `/api/v1` 上的最小实现边界。移动端复用 Web 的资源接口和 Pydantic 响应，不直接读取数据库、Manifest、对象存储或供应商接口。

## 会话和数据边界

- App 只使用 `POST /api/v1/auth/token`、`/auth/refresh` 和 `/auth/revoke` 的 Bearer 契约；access token 与 refresh token 只能保存在系统安全存储，不能进入普通偏好、日志、崩溃上报、剪贴板或截图文本。
- 日期是 `YYYY-MM-DD`，时间是带时区的 ISO 8601；金额、价格和比例沿用现有 JSON 数字/字符串表示，展示前不得擅自改变精度。
- 报告结论只来自服务端提交的快照。实时 `/market/*` 数据仅用于复核显示，必须标为实时或延迟，不能覆盖报告中的分数、证据、交易方案或 `decision_at`。

## 启动研究

1. 采集用户的交易日、`scope`、可选 `symbols` 和预算，调用 `POST /api/v1/research/runs`。每一次用户确认都生成并保留一个 `Idempotency-Key`，网络重试必须复用同一个键。
2. 在确认页提供 `supreme_mode=false|true`。默认标准模式；至高模式只请求自适应的数据采集并行，不表示模型、评分、风控、股票池或交易规则发生变化。
3. 成功响应是 `202 ResearchRunResponse`，复用运行是 `200 ResearchRunResponse`。保存 `run_id` 和首个响应的 `supreme_mode`，但不要把它当作已生效的资源档案。
4. 使用 `GET /api/v1/research/runs/{run_id}` 轮询。活跃状态按 2-5 秒退避轮询；进入 `SUCCEEDED`、`FUSED`、`FAILED` 或 `CANCELLED` 后停止。取消调用 `POST /api/v1/research/runs/{run_id}/cancel`，重复取消按服务端幂等语义处理。

`execution_profile` 在 Worker 开始数据同步后才可能出现。移动端应把它作为只读状态显示：`mode`、`data_fetch_workers`、`resource_level` 和 `reason_codes` 可用于解释本次是否被资源压力收敛；`model_agent_max_concurrency` 与 `model_concurrency_changed=false` 应明确显示为模型并发未被至高模式提升。该字段可为空，旧客户端可直接忽略新增字段。

## 报告工作台

完成或观察模式后，按运行 ID 获取下面的资源，保持请求独立、可重试：

| 目的 | 接口 | 移动端处理 |
|---|---|---|
| 报告头 | `GET /reports/{trading_date}?run_id=...` | 显示报告类型、生成时间和状态 |
| 报告正文 | `GET /reports/{report_id}/content` | 用受限 HTML/WebView 容器展示；禁止执行脚本、打开任意外链或暴露 `object_uri` |
| 逐股研究 | `GET /reports/{report_id}/symbols` | 使用服务端排序，保留 `advice_eligible`、`research_status` 和评分 |
| 冻结评分 | `GET /scores/{date}/{symbol}?run_id=...` | 显示确定性分数与公式版本 |
| 实时复核 | `/market/quotes`、`/market/klines` | 与冻结结论分区显示，保留来源和延迟标记 |
| 模拟方案 | `GET/POST /reports/{report_id}/trade-plans` | 只处理模拟组合；写入请求必须带新的 `Idempotency-Key` |

当研究为 `FUSED`、个股 `advice_eligible=false`、`research_status=RISK_BLOCKED` 或数据状态受限时，客户端必须隐藏/禁用生成模拟买入方案的入口，但仍可显示冻结报告、评分和风险说明。所有交易建议都是模拟组合建议，不能调用券商下单能力。

## 页面状态

建议将页面状态持久化为 `selectedDate + selectedRunId + selectedSymbol`，并以服务端响应为唯一真相：

```text
idle -> submitting -> queued/waiting -> running -> succeeded|fused|failed|cancelled
                                      -> cancel_requested -> cancelled|failed
```

`DATA_READINESS_WAITING` 应展示 `next_retry_at`，不要在客户端自行推断交易日、补数据或重建历史实时快照。`401` 触发一次 refresh 并重试原请求；refresh 失败后清空安全存储并回到登录。`403`、`422`、`429`、`503` 只展示服务端已脱敏信息，不显示堆栈、路径、供应商字段或请求中的 token。

## 兼容策略

- `/api/v1` 字段只做新增，客户端对未知字段忽略，对可空新增字段使用安全空状态。
- 列表、报告与 K 线请求必须传服务端允许的 `limit` 和 `run_id`，不依赖隐式“最新报告”。
- 记录本地草稿时不保存令牌、完整报告 HTML、审计详情、原始响应或模型提示词；只保存未提交的范围、预算和选择状态。
- 后续移动端设计应复用报告工作台的信息层级：报告总览、可追溯运行状态、逐股选择、冻结评分、实时复核和模拟方案。至高模式的视觉标识只反映已返回的 `supreme_mode`/`execution_profile`，不在客户端伪造资源状态。
