# 离线部署

离线包面向无互联网环境，包含 API、Web、LLM 模型 `gateway`、行情桥、单一 `job-worker`、
PostgreSQL 和 Redis 镜像，以及原生安装脚本。系统只支持研究、回测和模拟组合；不包含搜索服务、
联网新闻桥或 Edge Gateway。

## 系统要求

- Docker Engine 24+ 或 Docker Desktop 4+。
- 最低 2 核 CPU、4 GB 内存和 10 GB 可用磁盘。
- Windows 10/11 或常见 Linux 发行版。

## 安装

Windows：

```powershell
.\scripts\offline-install.ps1 -ImportImages
```

Linux：

```bash
chmod +x scripts/offline-install.sh
IMPORT_IMAGES=true ./scripts/offline-install.sh
```

安装脚本生成随机数据库和 Redis 密码。根据部署环境设置 `API_BIND_ADDRESS`、`WEB_BIND_ADDRESS`
和 `SERVICE_BIND_ADDRESS`，不要提交生成的 `.env`。

## 服务和检查

```bash
docker compose -p ashare-ai-src -f compose.yaml up -d
docker compose -p ashare-ai-src -f compose.yaml ps
curl http://localhost:8000/api/v1/health
```

预期核心服务为 `api`、`web`、`gateway`、`quote-bridge`、`job-worker`、`postgres` 和 `redis`。
健康响应只报告模型网关与行情桥状态；前端从同源 API 加载研究工作台。

## 数据备份

```bash
docker compose -p ashare-ai-src -f compose.yaml exec -T postgres \
  pg_dump -U ashare -d ashare -Fc > ashare-backup.dump
```

同时备份 PostgreSQL、Redis、lake、对象和私有数据卷。不要执行 `docker compose down -v`，否则会
删除这些卷。

## 限制

离线环境不能执行需要外部模型或行情供应商的请求。研究快照仍必须满足 `symbol`、`trading_date`、
`available_at` 和 `decision_at` 的 PIT 约束；低置信度 System-2 诊断在模型网关不可用时显示失败状态，
不会改变 Jev 的确定性裁决。
