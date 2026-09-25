# Docker 部署

本文档适用于单机 Linux 服务器。当前 Compose 栈包含 Web、API、模型 `gateway`、行情桥、单一
`job-worker`、PostgreSQL 和 Redis。系统只服务研究、回测和模拟组合，不提供实盘交易。

## 准备配置

```bash
git clone https://github.com/dengrb1/ashare-ai-research.git
cd ashare-ai-research
cp .env.local.example .env
cp .env.docker.example .env.docker
chmod 600 .env .env.docker
```

至少设置数据库、Redis、管理员和生产模型配置：

```env
APP_ENV=production
POSTGRES_PASSWORD=<随机密码>
REDIS_PASSWORD=<随机密码>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<独立强密码>
COOKIE_SECURE=true
TRUSTED_HOSTS=research.example.com
MODEL_ALLOWED_HOSTS=api.openai.com
MODEL_SETTINGS_ENCRYPTION_KEYS=<Fernet 密钥>
PRIVATE_OBJECT_ROOT=/data/private
CANONICAL_BUNDLE_MODE=akshare
ALLOW_DEMO_DATA=false
```

模型 API Key 通过管理员设置页保存，由后端加密、探测和审计；不要写入前端、日志或仓库文件。

## 启动和检查

```bash
docker compose -p ashare-ai-src -f compose.yaml config --quiet
docker compose -p ashare-ai-src -f compose.yaml up -d --build
docker compose -p ashare-ai-src -f compose.yaml ps
docker compose -p ashare-ai-src -f compose.yaml exec api ashare-ai doctor
docker compose -p ashare-ai-src -f compose.yaml logs --tail 100 api job-worker
```

默认只绑定本机地址。需要外部 TLS 时，在服务器前置 Caddy、Nginx 或云负载均衡器；仓库不再提供
Edge Gateway profile。

## 服务边界

- `gateway` 是 LLM 模型网关，监听 `8787`，与 Edge Gateway 概念不同。
- `quote-bridge` 只提供实时行情补充数据，监听 `8081`。
- `job-worker` 串行消费研究、回测、Trade Plan、个人档案、退出建议、Jev 训练和 System-2 队列。
- API 使用冻结研究快照生成评分；实时指数展示与研究快照隔离。

不启动搜索服务，也不提供联网新闻检索。研究结果必须携带 `symbol`、`trading_date`、`available_at`
和 `decision_at`。

## GHCR 镜像

```bash
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml pull
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml up -d
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml ps
```

可通过 `ASHARE_APP_IMAGE`、`ASHARE_WEB_IMAGE` 和 `ASHARE_POSTGRES_IMAGE` 指向私有镜像。

## 升级与备份

```bash
mkdir -p backups
docker compose -p ashare-ai-src -f compose.yaml exec -T postgres \
  pg_dump -U ashare -d ashare -Fc > "backups/ashare-$(date +%F-%H%M).dump"
git pull --ff-only
docker compose -p ashare-ai-src -f compose.yaml up -d --build
```

API 启动时执行向前迁移。不要执行 `docker compose down -v`，否则会删除数据库、Redis、lake 和
对象数据卷。

## 节能模式

管理员开启 `energy_saving_enabled` 后，收盘且没有活动任务时单一 `job-worker` 进入深度待机，
保留调度和维护 tick。宿主控制器可根据 `/api/internal/topology-desired` 的
`energy_saving_active` 停止或恢复 `job-worker`；API 不访问 Docker socket。
