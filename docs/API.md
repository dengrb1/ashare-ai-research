# A 股 AI 自动投研系统 API 文档

版本：`v1`  
基础路径：`/api/v1`  
契约来源：`src/ashare_ai/api/app.py`、`src/ashare_ai/api/schemas.py` 及 `src/ashare_ai/search/service.py`

本文档描述当前代码实际注册的公开 HTTP API。当前 OpenAPI 中有 55 个操作；另外保留了 5 个不在 OpenAPI 展示的兼容别名。接口只用于研究、评分、报告、回测和模拟组合，不执行真实下单。

## 1. 基本约定

### 1.1 地址、格式和时间

- 本地完整 Docker 栈：`http://127.0.0.1`，Web 通过 Nginx 代理 `/api`。
- 本地直接访问 API：`http://127.0.0.1:8000`。
- 所有请求和响应使用 `application/json`，报告内容接口除外。
- 日期使用 `YYYY-MM-DD`，例如 `2026-07-17`。
- 时间使用带时区的 ISO 8601，例如 `2026-07-17T08:00:00Z` 或 `2026-07-17T16:00:00+08:00`。
- A 股代码统一为 `6 位数字.SH|SZ|BJ`，例如 `600519.SH`、`000001.SZ`、`430047.BJ`。服务端会去空格并转为大写。
- 金额、预算和比例按字段语义返回；客户端不要依赖浮点数的二进制精度。Pydantic `Decimal` 字段的 JSON Schema 同时允许 JSON 数字或十进制字符串。
- API 响应默认带 `Cache-Control: no-store`；实时行情和研究快照不能混用。

### 1.2 权限标记

| 标记 | 含义 |
|---|---|
| `公开` | 不需要登录：健康检查和认证入口 |
| `登录` | 需要 Web Cookie 会话或 App Bearer 会话 |
| `写入` | 需要登录；Cookie 会话还需要 CSRF，Bearer 会话不需要 CSRF |
| `管理员` | 需要登录且 `role=ADMIN`；写入接口仍遵循 CSRF 规则 |

除特别说明外，普通用户只能访问自己创建的运行、报告、快照、回测和 Trade Plan；管理员可读取全体用户的结果，但不能通过客户端参数伪造资源归属。

### 1.3 认证方式

系统有两套互斥的会话类型：

1. Web：`POST /auth/login` 成功后返回用户对象，并通过 `Set-Cookie` 下发 HttpOnly 会话 Cookie 和可读的 CSRF Cookie。
2. 原生客户端：`POST /auth/token` 获取短期 access token 和 refresh token，后续请求使用 `Authorization: Bearer <access_token>`。

原生客户端不得模拟 Web Cookie 或 CSRF；令牌只能存储在 iOS Keychain、Android Keystore 等操作系统安全存储中，不得写入普通偏好、日志、崩溃报告、剪贴板或页面。

## 2. 快速开始

以下示例只使用占位值，不代表真实账号或令牌。

### 2.1 原生客户端登录和刷新

```bash
BASE_URL="http://127.0.0.1"

curl -sS "$BASE_URL/api/v1/auth/token" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<PASSWORD>"}'
```

成功响应：

```json
{
  "access_token": "<ACCESS_TOKEN>",
  "token_type": "bearer",
  "expires_in": 900,
  "refresh_token": "<REFRESH_TOKEN>",
  "refresh_expires_in": 2592000
}
```

刷新时必须保存响应中的新 refresh token；刷新接口会轮换旧 refresh token，旧值不能继续使用：

```bash
curl -sS "$BASE_URL/api/v1/auth/refresh" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"<REFRESH_TOKEN>"}'
```

随后调用初始化接口：

```bash
curl -sS "$BASE_URL/api/v1/app/bootstrap" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

### 2.2 Web Cookie 登录和 CSRF

```bash
curl -i -c cookies.txt "$BASE_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<PASSWORD>"}'
```

登录响应中的 `Set-Cookie` 包含 `ashare_session` 和 `ashare_csrf`。所有 Cookie 写请求都要把 `ashare_csrf` 的值原样放入 `X-CSRF-Token` 请求头：

```bash
curl -sS -b cookies.txt -c cookies.txt "$BASE_URL/api/v1/assets" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: <COOKIE_VALUE_OF_ashare_csrf>" \
  -X PUT \
  -d '{"watchlist":["600519.SH"],"positions":[],"total_assets":1000000}'
```

`GET`、`HEAD`、`OPTIONS` 不要求 CSRF。Bearer 写请求也不要求 CSRF，但仍要求有效的 App access token。

## 3. 端点总览

| 分组 | 方法 | 路径 | 权限 |
|---|---|---|---|
| 健康 | GET | `/api/v1/health` | 公开 |
| 认证 | POST | `/api/v1/auth/login` | 公开 |
| 认证 | POST | `/api/v1/auth/token` | 公开 |
| 认证 | POST | `/api/v1/auth/refresh` | 公开（提交 refresh token） |
| 认证 | POST | `/api/v1/auth/revoke` | 公开（提交 refresh token） |
| 认证 | POST | `/api/v1/auth/logout` | 登录、写入 |
| 认证 | GET | `/api/v1/auth/me` | 登录 |
| App 初始化 | GET | `/api/v1/app/bootstrap` | 登录 |
| 用户资产 | GET/PUT | `/api/v1/assets` | 登录/写入 |
| 退出监控设置 | PUT | `/api/v1/assets/exit-monitor` | 写入 |
| 卖出建议 | GET | `/api/v1/exit-advice` | 登录 |
| 卖出建议 | GET | `/api/v1/exit-advice/{advice_id}` | 登录 |
| AI 对话 | GET | `/api/v1/ai/models` | 登录 |
| AI 对话 | GET/POST | `/api/v1/ai/chat/threads` | 登录/写入 |
| AI 对话 | GET | `/api/v1/ai/chat/thread-index` | 登录 |
| AI 对话 | PATCH/DELETE | `/api/v1/ai/chat/threads/{thread_id}` | 写入 |
| AI 对话 | POST | `/api/v1/ai/chat/threads:bulk-delete` | 写入 |
| AI 对话 | GET | `/api/v1/ai/chat/threads/{thread_id}/messages` | 登录 |
| AI 对话 | POST | `/api/v1/ai/chat/threads/{thread_id}/messages:stream` | 写入、SSE |
| AI 图片 | POST | `/api/v1/ai/chat/attachments` | 写入、multipart |
| AI 图片 | GET | `/api/v1/ai/chat/attachments/{attachment_id}/content` | 登录、所有者 |
| 个人档案 | POST | `/api/v1/me/data-exports` | 写入、202 |
| 个人档案 | GET/DELETE | `/api/v1/me/data-exports/{export_id}` | 登录/写入、所有者 |
| 个人档案 | GET | `/api/v1/me/data-exports/{export_id}/download` | 登录、所有者 |
| 个人档案 | POST | `/api/v1/me/data-imports` | 写入、multipart、202 |
| 个人档案 | GET | `/api/v1/me/data-imports/{import_id}` | 登录、所有者 |
| 个人档案 | POST | `/api/v1/me/data-imports/{import_id}/apply` | 写入、幂等、202 |
| 用户管理 | GET/POST | `/api/v1/admin/users` | 管理员 |
| 用户管理 | PATCH | `/api/v1/admin/users/{user_id}` | 管理员 |
| 用户管理 | POST | `/api/v1/admin/users/{user_id}/password` | 管理员 |
| 模型设置 | GET/PUT | `/api/v1/admin/model-settings` | 管理员 |
| 模型设置 | POST | `/api/v1/admin/model-settings/test` | 管理员、写入 |
| 模型设置 | POST | `/api/v1/admin/model-settings/models` | 管理员、写入 |
| 研究结果 | GET | `/api/v1/scores/{trading_date}` | 登录 |
| 研究结果 | GET | `/api/v1/scores/{trading_date}/{symbol}` | 登录 |
| 研究结果 | GET | `/api/v1/scores/{trading_date}/{symbol}/lineage` | 登录 |
| 研究结果 | GET | `/api/v1/candidates/{trading_date}` | 登录 |
| 研究结果 | GET | `/api/v1/portfolios/{trading_date}` | 登录 |
| 报告 | GET | `/api/v1/reports/{trading_date}` | 登录 |
| 报告 | GET | `/api/v1/reports/{report_id}/content` | 登录 |
| 报告 | GET | `/api/v1/reports/{report_id}/symbols` | 登录 |
| Trade Plan | POST/GET | `/api/v1/reports/{report_id}/trade-plans` | 写入/登录 |
| Trade Plan | GET | `/api/v1/trade-plans/{plan_id}` | 登录 |
| 快照 | GET | `/api/v1/snapshots` | 登录 |
| 研究任务 | POST/GET | `/api/v1/research/runs` | 写入/登录 |
| 研究任务 | GET | `/api/v1/research/runs/{run_id}` | 登录 |
| 研究任务 | GET/PUT | `/api/v1/research/settings` | 登录/写入 |
| 研究任务 | POST | `/api/v1/research/runs/{run_id}/cancel` | 写入 |
| 通用运行 | GET | `/api/v1/runs` | 登录 |
| 通用运行 | GET | `/api/v1/runs/{run_id}` | 登录 |
| 通用运行 | GET | `/api/v1/runs/{run_id}/audit` | 登录 |
| 回测 | POST | `/api/v1/backtests` | 写入 |
| 回测 | GET | `/api/v1/backtests` | 登录 |
| 回测 | GET | `/api/v1/backtests/{backtest_id}` | 登录 |
| 回测 | POST | `/api/v1/backtests/{backtest_id}/retry` | 写入 |
| 行情 | GET | `/api/v1/market/quotes` | 登录 |
| 行情 | GET | `/api/v1/market/klines/{symbol}` | 登录 |
| 行情 | POST | `/api/v1/market/prefetch` | 写入 |
| 行情 | GET | `/api/v1/market/status` | 登录 |
| 财报检索 | GET | `/api/v1/search/financial` | 登录 |
| 财报检索 | GET | `/api/v1/search/status` | 登录 |

## 4. 认证接口

### `POST /api/v1/auth/login`

Web 登录。请求体为 `LoginRequest`：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---:|---|
| `username` | string | 是 | 1–64 字符 |
| `password` | string | 是 | 1–256 字符 |

返回 `200 UserResponse`，并下发 Web 会话 Cookie。失败返回 `401`；认证失败会按来源地址和账号执行限流，超限返回 `429` 和 `Retry-After`。

### `POST /api/v1/auth/token`

原生客户端登录。请求体同 `LoginRequest`，返回 `200 TokenResponse`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `access_token` | string | Bearer 访问令牌，不返回到日志或页面 |
| `token_type` | string | 固定为 `bearer` |
| `expires_in` | integer | access token 剩余秒数，默认 900 |
| `refresh_token` | string | 轮换用 refresh token |
| `refresh_expires_in` | integer | refresh token 剩余秒数，默认 2,592,000 |

### `POST /api/v1/auth/refresh`

请求体：`{"refresh_token":"<REFRESH_TOKEN>"}`。成功返回新的 `TokenResponse`；旧 refresh token 立即失效。无效、过期、已撤销或账号已禁用返回 `401`。

### `POST /api/v1/auth/revoke`

请求体同上。撤销 refresh token，成功返回 `204 No Content`。该接口按目标令牌执行幂等撤销，不要求先发送 access token；客户端退出登录或撤销设备时使用它。

### `POST /api/v1/auth/logout`

撤销当前登录会话并清除 Web Cookie，返回 `204 No Content`。需要有效会话；Cookie 会话需要 `X-CSRF-Token`，Bearer 会话不需要 CSRF。原生客户端优先使用 `/auth/revoke` 撤销 refresh token。

### `GET /api/v1/auth/me`

返回当前 `UserResponse`。未登录返回 `401`。

## 5. 通用请求和响应模型

### 5.1 用户、资产和初始化

`UserResponse`：

| 字段 | 类型 |
|---|---|
| `user_id` | string |
| `username` | string |
| `role` | string，通常为 `USER` 或 `ADMIN` |
| `enabled` | boolean |
| `created_at` | datetime |
| `updated_at` | datetime |

`PaperPosition`：

| 字段 | 类型 | 约束 |
|---|---|---|
| `symbol` | string | `^\d{6}\.(SH\|SZ\|BJ)$` |
| `name` | string | 默认空字符串，最多 64 字符 |
| `quantity` | integer | 1–1,000,000,000 |
| `cost` | number | 大于 0，最多 10,000,000 |
| `target_weight` | number/null | 0–1；兼容旧客户端，当前持仓权重由市值和总资产派生 |
| `acquired_on` | date/null | 买入日期；缺失时 AI 可研究但模拟卖出数量被 T+1 门禁阻断 |
| `profit_trigger_amount` | decimal/null | 旧客户端兼容字段；仅在未设置个股股价线时作为个股浮盈金额规则 |
| `exit_trigger_price` | decimal/null | 最新股价严格超过该元/股价格时触发，优先于金额规则 |

`AssetStateRequest`：

| 字段 | 类型 | 约束 |
|---|---|---|
| `watchlist` | string[] | 最多 100 个，不重复 |
| `positions` | PaperPosition[] | 最多 15 个，代码不重复 |
| `total_assets` | number/null | 省略时保留原值；传 `null` 可清空，传值必须大于 0且不超过 1,000,000,000,000 |
| `exit_monitor_enabled` | boolean | 是否启用交易时段每 5 分钟盈利监控 |
| `default_profit_trigger` | decimal/null | 全局人民币浮盈触发金额；启用监控时应提供 |

`AssetStateResponse` 在此基础上增加 `updated_at`；`GET /assets` 返回当前用户数据，`PUT /assets` 整体替换当前用户自选股、模拟持仓和可选总资产。

`PUT /api/v1/assets/exit-monitor` 只接受 `exit_monitor_enabled` 和
`default_profit_trigger`，不会修改自选股、模拟持仓或总资产。手机客户端调整监控设置时应优先使用该窄接口，避免用旧缓存整体覆盖资产数据。

`GET /api/v1/app/bootstrap` 返回：

```json
{
  "server_time": "2026-07-19T12:00:00Z",
  "user": {"user_id":"<USER_ID>","username":"admin","role":"ADMIN","enabled":true,"created_at":"<DATETIME>","updated_at":"<DATETIME>"},
  "assets": {"watchlist":[],"positions":[],"total_assets":null,"updated_at":null},
  "capabilities": {
    "api_version":"v1",
    "authentication":"BEARER_REFRESH",
    "supported_research_scopes":["MARKET","WATCHLIST","CUSTOM"],
    "max_watchlist_symbols":100,
    "max_research_symbols":100,
    "max_trade_plan_symbols":15,
    "portfolio_target_count":10,
    "features": {"watchlist_research_selection":true,"formal_watchlist_reports":true,"report_symbol_eligibility":true,"trade_plan_generation":true,"research_cancellation":true,"idempotency_key":true,"paper_portfolio_only":true,"persistent_ai_chat":true,"chat_images_seven_day_retention":true,"personal_archive_export_import":true},
    "endpoints": {"assets":"/api/v1/assets","exit_monitor_settings":"/api/v1/assets/exit-monitor","research_runs":"/api/v1/research/runs","research_run":"/api/v1/research/runs/{run_id}","research_settings":"/api/v1/research/settings","ai_chat_threads":"/api/v1/ai/chat/threads","ai_chat_thread_index":"/api/v1/ai/chat/thread-index","personal_data_exports":"/api/v1/me/data-exports","personal_data_imports":"/api/v1/me/data-imports"}
  }
}
```

`portfolio_target_count` 从版本化策略配置读取，不应由客户端写入或覆盖。

### 5.2 卖出建议与 AI 对话

个股已设 `exit_trigger_price` 时，最新价必须严格超过该价格才提交研究；否则按 `(最新价 - 成本价) × 持股数` 与旧个股金额或全局 `default_profit_trigger` 比较。`ExitAdviceResponse` 新增 `trigger_type=PRICE|PROFIT_AMOUNT` 和 `trigger_price`；价格规则的 `trigger_amount` 仍返回等价浮盈金额，供旧客户端继续读取。同一用户、股票和交易日内，仅当价格相对上次建议变化至少 3%、持仓变化或正式评分变化时重新调用 AI。

`ExitAdviceResponse.result` 包含 `action=HOLD|REDUCE|SELL`、`summary`、`confidence`、`sell_ladder[]`、`stop_loss_price`、`risks`、`sellable_quantity`、`execution_blockers` 和 `paper_trade_only=true`。每档包含 `target_price`、`quantity`、`estimated_gross_proceeds`、`reason` 与 `status`。缺少买入日期、T+1 未满足、证券主数据或带生效日期交易规则不可用时，档位状态为阻断，不得据此修改模拟持仓。

AI 对话线程和消息均按当前用户保存。发送请求增加 `attachment_ids`、经证券主数据逐项核对名称与代码绑定的 `mention_refs[{symbol,name}]`、可选带时区 `decision_at` 及 `Idempotency-Key`；服务端拒绝未来时点并为本次调用冻结权威 `decision_at`。消息响应新增 `trading_date`、`decision_at`、`available_at`。正式评分只读取 `ScoreRow.decision_at <= 本次决策时点` 的当前用户成功运行，K 线使用相同截止时点，缺少可验证抓取时点的报价不会进入上下文。

助手历史在 Responses API 中编码为 `output_text`，用户/系统文本为 `input_text`，未到期的近期用户图片为 `input_image`；每张已销毁图片分别加入不可用标记。SSE 会先发 `meta` 事件，随后发 `delta`、`done` 或安全 `error`；错误含 `code`、`request_id`和 `retryable`。只有收到上游 `response.completed` 后才写入完成状态和缓存。浏览器与模型网关均仅在首个正文片段之前，对网络错误、408、429、5xx 或明确可重试错误使用同一幂等键进行有限重试；正文出现后断线会保留部分内容并标记未完成。

`POST /ai/chat/attachments` 支持 PNG、JPEG、WebP 和非动画 GIF；每条最多 4 张、单张 10 MB、合计 25 MB。服务端校验真实签名、MIME、尺寸和动画状态，不接受远程 URL。`expires_at=uploaded_at+7天` 固定不延长；到期瞬间读取返回 `410`，后台五分钟内物理清理。

### 5.3 个人档案

导出、导入均只处理当前登录用户的资源。导出与导入预览首次提交返回 `202` 和 `archive_id`，状态包含 `PENDING|PROCESSING|SUCCEEDED|FAILED|CANCELLED`、`phase`、`progress` 与 24 小时 `expires_at`。预览的 `history.classification` 按聊天线程、聊天消息、研究运行、报告和回测分别列出 `new|duplicate|conflict`，每项包含来源 ID 与规范化哈希；用户确认前即可看到后续跳过或重映射范围。导入应用要求 `Idempotency-Key`；同键同请求返回首个任务，同键异请求返回 `409`。

导出包不包含任何图片、图片 URI/文件名/内容哈希、密码、角色、会话、Token、API Key、响应缓存或服务器路径。导入还要求有效服务端来源认证，拒绝用户自行构造的派生评分、运行和 Manifest。格式、加密及合并细节见 [`docs/PERSONAL_ARCHIVE.md`](PERSONAL_ARCHIVE.md)。

流式接口使用 `text/event-stream`，事件依次为 `meta`、多个 `delta`、`done`，失败时为 `error`。Nginx 缓冲通过 `X-Accel-Buffering: no` 禁用；客户端必须处理断线且不得把 Cookie、Bearer token 或完整敏感持仓写入日志。

### 5.3 研究、运行和回测

`RunResponse`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | string | 运行 ID |
| `run_type` | string | 例如 `DAILY`、`BACKTEST` |
| `trading_date` | date | 研究或回测关联日期 |
| `decision_at` | datetime | 服务端冻结的决策时点 |
| `status` | string | 状态字符串 |
| `input_hash` | string | 输入复现哈希 |
| `output_hash` | string/null | 完成后产物哈希 |
| `started_at` | datetime | 创建/启动时间 |
| `completed_at` | datetime/null | 完成或失败时间 |
| `error_message` | string/null | 已脱敏的失败原因 |

`ResearchRunResponse` 继承 `RunResponse`，并增加：`phase`、`progress`（0–100）、`report_id`、`report_type`、`report_created_at`、`research_scope`、`target_symbols`、`total_budget`、`per_symbol_budget`、`max_stock_price`、`portfolio_requested`、`portfolio_generated`、`reason_code`、`reason_message`、`formal_eligible_count`、`excluded_symbol_count`、`portfolio_reason_code`、`portfolio_reason_message`、`trigger_source`（`AUTO|MANUAL`）、`automatic_report_slot`（自动任务为 `A|B`，手动任务为 null）和 `requested_date`。

`BacktestRequest`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `name` | string | 是 | 1–128 字符 |
| `start_date` | date | 是 | 回测开始日期 |
| `end_date` | date | 是 | 必须不早于 `start_date` |
| `snapshot_ids` | string[] | 是 | 当前必须恰好 1 个已提交、可执行的累计 `backtest_bundle` 快照 |
| `config` | object | 否 | 当前支持 `initial_capital`（默认 1,000,000）和 `benchmark`（默认 `000300.SH`），其余由回测执行器解释 |

`BacktestResponse`：`backtest_id`、`run_id`、`status`、`name`、`start_date`、`end_date`、`snapshot_ids`、`metrics`、`artifacts`、`input_hash`、`output_hash`、`retry_count`、`created_at`、`completed_at`、`error_message`。

### 5.3 研究请求和自动研究设置

`ResearchRequest`：

| 字段 | 类型 | 默认/约束 |
|---|---|---|
| `trading_date` | date | 必填；未来日期拒绝 |
| `scope` | string | `MARKET`、`WATCHLIST`、`CUSTOM`，默认 `MARKET` |
| `symbols` | string[] | 最多 100 个，不重复；`CUSTOM` 必填，`MARKET` 不允许传 |
| `total_budget` | decimal/null | 大于 0且不超过 100,000,000,000 |
| `per_symbol_budget` | decimal/null | 大于 0且不超过 100,000,000,000，不能大于总预算 |
| `max_stock_price` | decimal/null | 大于 0且不超过 10,000,000 |

`WATCHLIST` 的 `symbols` 可以省略，表示当前用户全部自选股与模拟持仓；如果显式传入，所有代码必须属于当前用户的自选股或持仓。`CUSTOM` 必须至少包含一只代码。非交易日会解析为可用的最近完成交易日；若数据未就绪返回 `409`。

`ResearchSettingsRequest` 接受以下两种互斥形态：

- 新客户端提交 `automatic_reports`，必须恰好包含槽位 `A`、`B` 各一次。每项包含 `slot`、`enabled`、`scope`（`MARKET|WATCHLIST|CUSTOM`）、`symbols`、`total_budget`、`per_symbol_budget`、可空 `max_stock_price`；预算校验与手动研究相同，`CUSTOM` 必须提供有效股票代码。
- 兼容旧客户端的 `auto_enabled: boolean` 仍保留。`true` 启用报告 A，`false` 关闭 A/B，已保存的范围和预算不会丢失。同一请求不得同时提交两个字段。

`ResearchSettingsResponse` 返回 `automatic_reports` 固定 A/B 数组；`auto_enabled` 表示至少一套配置启用。旧字段 `automatic_scope`、`automatic_total_budget`、`automatic_per_symbol_budget`、`automatic_max_stock_price` 继续返回报告 A 的值，并同时返回 `updated_at`、`schedule_timezone=Asia/Shanghai`、`schedule_time=15:05`、`snapshot_mode=SYSTEM_ENFORCED` 和 `portfolio_target_count`。

### 5.4 Trade Plan 请求

`TradePlanRequest`：

| 字段 | 类型 | 默认/约束 |
|---|---|---|
| `symbols` | string[] | 必填，1–15 个，不重复；服务端排序并校验 |
| `budget_override` | decimal/null | 可选，大于 0且不超过 100,000,000,000 |
| `objective` | string | 当前固定为 `RISK_ADJUSTED_RETURN` |

Trade Plan 只接受报告中通过个股数据门禁、事件风险门禁和验证历史门禁的股票；结果仅用于研究和模拟盘。

## 6. 管理接口

### 6.1 用户管理

#### `GET /api/v1/admin/users`

按用户名排序返回 `UserResponse[]`。仅管理员可用。

#### `POST /api/v1/admin/users`

创建用户，成功返回 `201 UserResponse`。请求体 `UserCreateRequest`：

```json
{"username":"researcher_01","password":"<AT_LEAST_12_CHARACTERS>","role":"USER"}
```

`username` 只能包含 ASCII 字母、数字、下划线、点和连字符，密码 12–256 字符，`role` 为 `USER|ADMIN`。重复用户名返回 `409`。

#### `PATCH /api/v1/admin/users/{user_id}`

请求体字段均可选：`enabled: boolean|null`、`role: USER|ADMIN|null`、`password: string|null`（12–256 字符）。禁用、改角色或改密码会撤销该用户所有会话；不能禁用当前管理员或把当前管理员降级，违规返回 `409`。

#### `POST /api/v1/admin/users/{user_id}/password`

请求体 `{"password":"<AT_LEAST_12_CHARACTERS>"}`，修改密码并撤销该用户所有会话。

### 6.2 模型设置

`GET /api/v1/admin/model-settings` 返回当前运行配置，但只返回 `api_key_configured` 布尔值，不返回 API Key。`PUT`、`POST /test` 和 `POST /models` 都使用 `ModelSettingsRequest`：

| 字段 | 类型 | 默认/约束 |
|---|---|---|
| `base_url` | string | 必填，8–2048 字符；服务端规范化为 `/v1` |
| `api_key` | string/null | 可选；空字符串表示沿用已保存密钥 |
| `search_model` | string | 默认 `gpt-5.6-luna` |
| `search_reasoning_effort` | string | `low|medium|high|xhigh`，默认 `low` |
| `research_model` | string | 默认 `gpt-5.6-sol` |
| `research_reasoning_effort` | string | `low|medium|high|xhigh`，默认 `high` |
| `timeout_seconds` | number | 1–600，默认 90 |
| `enabled` | boolean | 默认 `true` |

生产环境的 `base_url` 必须为 HTTPS，主机必须在 `MODEL_ALLOWED_HOSTS` 白名单中，不能携带账号、密码、查询字符串或片段。启用配置前服务端会执行结构化输出探测，失败返回 `422` 且旧配置继续生效。

`POST /api/v1/admin/model-settings/test` 返回 `ModelProbeResponse`：`reachable`、`message`、`model`、`checked_at`。`POST /api/v1/admin/model-settings/models` 返回 `{"models":["..."]}`。

## 7. 研究结果、报告和 Trade Plan

所有结果查询都支持可选查询参数 `run_id`，用于选择指定运行。省略时服务端按当前用户可见范围选择最新合适结果；管理员可跨用户查询，但不能读取不存在或无权限的运行。

### `GET /api/v1/scores/{trading_date}`

返回 `ScoreResponse[]`。`ScoreResponse` 字段为：`symbol`、`trading_date`、`decision_at`、`fundamental_score`、`technical_score`、`sentiment_score`、`quality_confidence_score`、`base_total_score`、`dividend_bonus`、`event_risk_multiplier`、`total_score`、`formula_version`、`agent_bundle_sha256`、`evidence_bundle_sha256`、`feature_snapshot_id`。

### `GET /api/v1/scores/{trading_date}/{symbol}`

返回单只股票的 `ScoreResponse`；不存在返回 `404`。

### `GET /api/v1/scores/{trading_date}/{symbol}/lineage`

返回评分谱系对象：

```json
{
  "symbol":"600519.SH",
  "trading_date":"2026-07-17",
  "run_id":"<RUN_ID>",
  "feature_snapshot_id":"<SNAPSHOT_ID>",
  "agent_bundle_sha256":"<SHA256>",
  "evidence_bundle_sha256":"<SHA256>",
  "formula_version":"<VERSION>",
  "base_total_score":80.0,
  "dividend_bonus":2.0,
  "event_risk_multiplier":1.0,
  "evidence":[
    {
      "evidence_id":"<ID>","component":"fundamental","evidence_type":"financial",
      "source":"<SOURCE>","source_record_id":"<RECORD_ID>",
      "available_at":"2026-07-17T06:00:00Z","payload_sha256":"<SHA256>",
      "excerpt":"<SANITIZED_EXCERPT>","object_uri":"<OBJECT_URI_OR_NULL>"
    }
  ]
}
```

谱系中的 `available_at` 必须不晚于对应 `decision_at`；客户端不要把 `object_uri` 拼接为外部下载地址。

### `GET /api/v1/candidates/{trading_date}`

返回按研究结果排序的 `CandidateResponse[]`，字段为：`symbol`、`trading_date`、`decision_at`、`rank`、`total_score`、`base_total_score`、`dividend_bonus`、`prediction_percentile`、`industry_code`、`event_risk_multiplier`、`style_exposures`、`evidence_hash`。

### `GET /api/v1/portfolios/{trading_date}`

返回 `PortfolioResponse`：`portfolio_id`、`run_id`、`trading_date`、`effective_trading_date`、`status`、`expected_turnover`、`cash_weight`、`constraint_version`、`input_hash`、`positions`、`rejection_reasons`、`observation_only`、`research_only`、`message`、`reason_code`、`formal_eligible_symbols`、`excluded_symbols`。

`FUSED` 研究返回 `observation_only=true`，只能查看评分、候选和报告，不生成模拟组合；小规模定向研究可能返回 `research_only=true`，表示研究完成但没有达到版本化组合目标数。

### `GET /api/v1/reports/{trading_date}`

返回 `ReportResponse`：`report_id`、`run_id`、`trading_date`、`report_type`、`object_uri`、`content_sha256`、`created_at`。

### `GET /api/v1/reports/{report_id}/content`

按需读取报告正文，返回 `ReportBodyResponse`：`report_id`、`content_type`（当前为 `text/html`）和 `content`。内容不可变且经过服务端对象路径校验；对象不可用时返回 `503`。

### `GET /api/v1/reports/{report_id}/symbols`

返回逐股票 `ReportSymbolResponse[]`：`symbol`、`name`、`research_status`（`FORMAL`、`FORMAL_WITH_LIMITATIONS`、`RISK_BLOCKED`）、`advice_eligible`、`recommendation`（不符合资格时固定为 `NO_BUY`）、`exclusion_reasons`、`data_quality`、嵌套 `score`、`rank`、`prediction_percentile`、`industry_code`。

只有 `advice_eligible=true` 的股票才允许提交 Trade Plan；客户端必须展示门禁原因，不得把 `NO_BUY` 转换成买入提示。

### `POST /api/v1/reports/{report_id}/trade-plans`

请求体为 `TradePlanRequest`，成功创建返回 `202 TradePlanResponse`。响应包含：`plan_id`、`user_id`、`report_id`、`run_id`、`trading_date`、`decision_at`、`available_at`、`status`、`objective`、`symbols`、`budget_override`、`snapshot_ids`、`optimizer_version`、`config_version`、`prompt_version`、`deterministic_result`、`ai_explanation`、`input_hash`、`output_hash`、`object_uri`、`object_sha256`、`created_at`、`started_at`、`completed_at`、`error_message`。

研究尚未成功、全局风控熔断、股票不在报告范围、数据不完整、重大事件风险或验证历史不足时返回 `409`；队列不可用返回 `503`。相同用户对相同报告和参数重复提交进行中的任务时返回既有任务，状态码为 `200`。

### `GET /api/v1/reports/{report_id}/trade-plans`

按创建时间倒序返回该报告的 `TradePlanResponse[]`。

### `GET /api/v1/trade-plans/{plan_id}`

返回单个 Trade Plan；普通用户只能读取自己的计划。

## 8. 研究任务和运行审计

### `POST /api/v1/research/runs`

提交 `ResearchRequest`，成功返回 `202 RunResponse`。响应中的 `run_id` 用于轮询：

```bash
curl -sS "$BASE_URL/api/v1/research/runs/<RUN_ID>" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

提交相同的用户、日期、范围、标的和预算且已有进行中任务时返回既有运行，状态码为 `200`。队列不可用返回 `503`；交易时段、未来日期或不安全的历史实时重建返回 `409`。收盘后若股票日线已到而任一策略基准（CSI300、CSI500、CSI1000）尚未覆盖目标日，仍创建 `202` 任务，状态为 `DATA_READINESS_WAITING`；轮询研究详情会返回“等待基准数据同步”以及新增的 `next_retry_at`。系统在冻结的原始范围、预算、价格上限、幂等键和活动键不变的前提下按固定间隔重试；在下一交易日开盘前仍不完整时以脱敏操作性原因终止，且绝不回退到开盘后的上一日实时快照。实时 AKShare 模式只允许冻结当日已就绪数据，或在下一交易日开盘前/非交易日冻结最近已完成交易日；冻结文件模式仍可按文件覆盖日期运行历史研究。

### `GET /api/v1/research/runs`

查询参数：

| 参数 | 默认 | 约束 |
|---|---:|---|
| `limit` | 5 | 1–50 |
| `trading_date` | 无 | `YYYY-MM-DD` |
| `mine` | false | 管理员传 `false` 可查看全体；普通用户始终只能查看自己 |
| `published` | false | 为 true 时只返回 `SUCCEEDED` 或 `FUSED` |

返回 `ResearchRunResponse[]`，排序为最新启动优先；`published=true` 时按完成时间优先。

### `GET /api/v1/research/runs/{run_id}`

返回单个 `ResearchRunResponse`，包含 `phase` 和 0–100 的 `progress`，供 Web 与手机客户端按 ID 轮询。普通用户只能读取自己的研究任务；不存在、非研究任务或无权限资源均返回 `404`。

### `PUT /api/v1/research/settings`

新客户端完整提交 `{"automatic_reports":[报告A,报告B]}`；旧请求 `{"auto_enabled":true|false}` 继续兼容。自动研究固定使用上海时区和 `15:05` 调度时间，快照模式不可修改。启用一个槽位即运行单报告，两个均启用则同日提交两份独立任务，由默认串行 Worker 依次执行。`WATCHLIST` 在每次运行时动态读取用户当时的自选股与模拟持仓；为空时只跳过该槽位并在两小时窗口内继续重试，不阻塞另一槽位。同一用户、交易日和槽位只接受首次提交，首次提交后修改配置或自选与持仓不会在当日重复创建报告，新配置从下一交易日起生效。

### `GET /api/v1/research/settings`

返回当前用户自动研究设置以及固定 A/B 两套配置。两套配置即使内容完全相同，也会使用不同槽位幂等键生成两份独立报告。

### `POST /api/v1/research/runs/{run_id}/cancel`

只能由任务所有者调用。排队任务进入 `CANCELLED`；运行中的任务进入 `CANCEL_REQUESTED`，在当前原子阶段结束后停止。已完成、失败、熔断、已取消或已经请求取消的任务返回 `409`。成功返回更新后的 `ResearchRunResponse`。

### `GET /api/v1/runs`

查询参数 `run_type`（可选，服务端转大写）和 `limit`（默认 100，范围 1–500）。返回 `RunListResponse[]`，每项在 `RunResponse` 上增加 `user_id`。

### `GET /api/v1/runs/{run_id}`

返回单个 `RunResponse`。`started_at` 和 `completed_at` 均为带时区 ISO 8601 时间；终态任务的界面应优先显示 `completed_at`。无权限资源统一返回 `404`，不泄漏资源是否存在；`error_message` 仅返回脱敏后的操作性摘要，不包含 SQL、数据库约束、服务端路径、远端地址或凭据。

### `GET /api/v1/runs/{run_id}/audit`

返回 `AuditEventResponse[]`，每项包含 `event_id`、`run_id`、`event_type`、`severity`、`message`、`details` 和 `created_at`。审计数据只按需读取，不应写入普通移动端缓存。

## 9. 快照和回测

### `GET /api/v1/snapshots`

查询参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `dataset` | `backtest_bundle` | 数据集名称 |
| `include_non_executable` | false | 仅管理员可以设为 true |

只返回状态为 `COMMITTED` 且关联研究状态为 `SUCCEEDED`/`FUSED` 的快照。普通用户只看到自己研究生成的快照；默认还会过滤掉 `executable_signal_count < 1` 的快照。结果按提交时间倒序。`SnapshotResponse` 字段为 `snapshot_id`、`dataset`、`source`、`fetched_at`、`row_count`、`status`、`details`。

### `POST /api/v1/backtests`

提交 `BacktestRequest`，返回 `202 BacktestResponse`。服务端会校验：

- `start_date <= end_date`；
- 只选择一个已提交、可执行的累计回测快照；
- 回测区间不能超出快照日历范围七天以上；
- 快照包含至少一个带后续执行交易日的 PIT 信号；
- 快照 Parquet 文件哈希与 Manifest 一致。

相同用户重复提交相同输入时返回已有结果，状态码为 `200`。

### `GET /api/v1/backtests`

查询参数 `limit` 默认 100，范围 1–500；返回当前用户（管理员为全体）的 `BacktestResponse[]`，按创建时间倒序。

### `GET /api/v1/backtests/{backtest_id}`

返回单个回测。

### `POST /api/v1/backtests/{backtest_id}/retry`

只能重试状态为 `FAILED` 的自己的回测。服务端会重新校验快照状态、Parquet 文件哈希和 Manifest 后重新排队，返回 `202`；非失败状态返回 `409`，队列不可用返回 `503`。

## 10. 行情接口

实时行情是交互数据，不写入研究或回测快照。行情响应中的 `status` 包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | string | 实际数据源 |
| `collected_at` | datetime | 上游抓取时间 |
| `cached_at` | datetime | 服务端缓存时间 |
| `delayed` | boolean | 是否使用延迟缓存 |
| `stale` | boolean | 当前通常与 `delayed` 同步 |
| `message` | string/null | 上游或缓存说明 |

### `GET /api/v1/market/quotes`

必填查询参数 `symbols`，使用逗号分隔，例如 `600519.SH,000001.SZ`；可选 `refresh=true|false` 强制刷新。返回 `QuoteResponse[]`，每项包含 `symbol`、`name`、`price`、`change`、`change_percent`、`open`、`high`、`low`、`previous_close`、`volume`、`amount` 和 `status`。单个数值可能为 `null`。

### `GET /api/v1/market/klines/{symbol}`

查询参数：

| 参数 | 默认 | 约束 |
|---|---|---|
| `period` | `day` | `1m`、`5m`、`15m`、`30m`、`60m`、`day`/`daily` |
| `limit` | 300 | 1–5000 |
| `adjust` | `hfq` | 当前只支持后复权 `hfq` |
| `start` | 无 | 带时区 datetime |
| `end` | 无 | 带时区 datetime |
| `refresh` | false | 是否绕过新鲜缓存 |

返回 `KlineResponse`：`symbol`、`period`、`adjustment`、`bars`、`status`。每个 `bar` 包含 `timestamp`、`open`、`high`、`low`、`close`、`volume`、`amount` 和 `turnover_rate`。

不支持的周期或复权方式返回 `422`；所有上游和可用缓存都失败时返回 `503`。

### `POST /api/v1/market/prefetch`

请求体：

```json
{"symbols":["600519.SH","000001.SZ"],"periods":["day"],"limit":160}
```

`symbols` 至少 1 个，去重后最多 50 个；`periods` 当前只能为 `day` 或 `daily`，服务端会规范为 `day`；`limit` 范围 1–5000。返回 `quotes`、`klines` 和按代码记录的 `errors`。单只股票失败不会丢弃其他成功结果。

### `GET /api/v1/market/status`

返回当前行情服务状态对象，主要字段包括 `primary`、`fallback`、`fallbacks`、`cache_seconds`、`kline_cache_seconds`、`prefetch_max_workers`、`prefetch_max_symbols`、`stale_seconds`、`adjustment`、`live_data_isolated_from_snapshots` 和可选 `quotes` 缓存状态。

## 11. 财务检索

### `GET /api/v1/search/financial`

必填查询参数 `q`，长度 1–256，例如：

```bash
curl -sS "$BASE_URL/api/v1/search/financial?q=%E8%B4%B5%E5%B7%9E%E8%8C%85%E5%8F%B0%E8%82%A1%E4%BB%B7" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
```

返回 `FinancialSearchResponse`：`query`、`provider`、`upstream`、`mode`（`cli|embedded|direct|ai`）、`searched_at`、`elapsed_ms`、`entities`、`recalls`、`raw_sha256`、`outcome`、`interpretation`、`sources`、`warnings`、`live_data_isolated_from_snapshots`。

`entities` 的元素包含 `name`、`code`；`recalls` 的元素包含 `type`、`desc`、`content`。检索结果是实时交互数据，不进入冻结研究、确定性评分或回测快照。

每个用户默认每分钟最多 30 次；超限返回 `429` 和 `Retry-After: 60`。上游并发繁忙返回 `429`，超时返回 `504`，上游不可用返回 `503`。

### `GET /api/v1/search/status`

返回 `FinancialSearchStatus`：`provider`、`upstream`、`mode`、`available`、`configured`、`reachable`、`degraded`、`model`、`script_path`、`message`、`live_data_isolated_from_snapshots`。出于安全原因，服务端当前不向远程客户端暴露服务器脚本路径，`script_path` 通常为 `null`。

## 12. 错误、限流和异步约定

### 12.1 HTTP 状态码

| 状态码 | 含义 |
|---:|---|
| `200` | 查询成功，或重复提交命中已有任务/结果 |
| `201` | 用户创建成功 |
| `202` | 研究、回测、Trade Plan 或回测重试已接受并异步排队 |
| `204` | 注销或撤销成功，无响应体 |
| `401` | 未登录、令牌无效、令牌过期或会话失效 |
| `403` | CSRF 无效或需要管理员权限 |
| `404` | 资源不存在或当前用户无权访问；不区分两者 |
| `409` | 状态冲突、重复提交冲突、研究日期无可用数据、门禁阻断或不可重试 |
| `422` | JSON/Pydantic 校验失败或业务参数不合法 |
| `429` | 认证或财务检索限流/繁忙 |
| `503` | 数据源、对象存储、任务队列或模型服务不可用 |
| `504` | 财务检索上游超时 |

### 12.2 错误响应格式

业务异常通常为：

```json
{"detail":"human-readable error message"}
```

请求校验错误通常为 FastAPI/Pydantic 格式：

```json
{
  "detail": [
    {"loc":["body","symbols"],"msg":"Field required","type":"missing"}
  ]
}
```

客户端应按 HTTP 状态码和 `detail` 处理，不依赖内部异常类型、堆栈、文件路径、数据库地址或供应商原始错误。

### 12.3 异步轮询

提交研究、回测或 Trade Plan 后，先保存返回的 `run_id`、`backtest_id` 或 `plan_id`，再使用对应详情接口查询。研究任务使用 `/research/runs/{run_id}`，不要用缺少 `phase/progress` 的通用 `/runs/{run_id}` 代替。不要依赖长连接或固定等待时间。常见研究状态包括 `PENDING`、`QUEUED`、`RUNNING`、`PROCESSING`、`SUCCEEDED`、`FAILED`、`FUSED`、`CANCEL_REQUESTED` 和 `CANCELLED`；具体 `phase` 和进度以响应为准。

研究、回测和报告 Trade Plan 提交支持 `Idempotency-Key` 请求头（1–128 字符）。服务端只持久化 Key 的 SHA-256，并按“用户 + 路由 + Key + 请求体哈希”去重：同 Key、同请求返回首次创建的资源和 `200`；同 Key、不同请求返回 `409`。首次接受仍返回 `202`。手机客户端应为每次用户意图生成随机 Key，在网络重试中原样复用，并保存返回的资源 ID继续轮询。

## 13. 兼容别名和开发工具

以下路径仍由代码保留，但不在 OpenAPI 文档中展示，新增客户端不应优先使用：

- `/api/v1/users` 对应 `/api/v1/admin/users` 的 GET/POST；
- `/api/v1/users/{user_id}` 对应管理员用户 PATCH；
- `/api/v1/users/{user_id}/password` 对应管理员密码重置；
- `/api/v1/market/kline/{symbol}` 对应 `/api/v1/market/klines/{symbol}`。

非生产环境可访问：

- Swagger UI：`GET /docs`；
- ReDoc：`GET /redoc`；
- OpenAPI JSON：`GET /openapi.json`。

生产环境会关闭这些公共入口；客户端契约应以本文件和版本化代码为准。
