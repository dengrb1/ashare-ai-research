# 分支更新说明

- 分支：`codex/research-model-and-mobile-push`
- 目标分支：`main`
- 整理日期：2026-07-29

## 更新内容

1. **移动端推送**：新增小米推送设备注册、注销和投递回执 API；设备注册 ID 使用个人数据加密密钥保存，维护任务通过事务投递表重试发送，响应不回传敏感注册 ID。
2. **交易建议监控**：新增自选股交易建议监控和 `0024_trade_advice_monitors` 迁移，支持买入目标、卖出目标、止损价、AI 价格和人工价格；只生成模拟交易通知，不会自动修改持仓或下单。
3. **通知与退出分析**：补充通知详情接口、推送回执、交易建议页面和提醒状态；增强流式退出研究的失败恢复、降级和重试路径，并完善模型状态与聊天通知展示。
4. **边缘网关与拓扑控制**：新增可选的低内存 HTTPS/ACME/FRP `edge` Compose profile，以及 Windows、Linux、macOS 的跨平台拓扑控制器；默认仍关闭公网边缘网关，控制器只同步已保存的系统设置。
5. **模型配置与部署**：强化 OpenAI 兼容模型的配置校验、并发恢复和不可用状态处理，补充环境示例、Docker 部署说明、API 契约、前端类型和回归测试。

## 验证结果

- 分支相关后端单元测试全部通过：移动推送、模型设置、OpenAI 兼容、退出建议、部署配置、系统设置、迁移和 Web 鉴权/行情测试。
- 前端 `npm test -- --run`：13 个测试文件、79 个测试通过。
- 前端 `npm run build`、`ruff check src tests`、`mypy src` 全部通过；Vite 仅提示产物 chunk 大于 500 kB。
- `docker compose -p ashare-ai-src -f compose.yaml up -d --build`、容器内 `ashare-ai doctor` 和 `ashare-ai migrate` 通过。
- 默认 Compose 项目的 API、Web、PostgreSQL、Redis、SearXNG、`job-worker` 和 `exit-advice-worker` 均为 healthy；Web `/` 与 API `/api/v1/health` 均返回 200。

完整 `python -m pytest -q` 以及完整 `tests/unit` 在本机 6 分钟内无输出并超时，未产生失败断言；已通过本分支直接涉及的测试组和 Docker 链路，后续可在具备完整外部服务的 CI 环境继续运行全套测试。
