# Docker 服务器部署教程

本文档面向单机 Linux 服务器。默认栈包含 Web、API、串行 Worker、PostgreSQL、Redis 和独立 SearXNG。SearXNG 与应用部署在同一个 Compose 私有网络，不依赖家中电脑，也不对公网暴露搜索端口。

## 1. 服务器准备

建议配置：

- Linux x86_64 服务器，2GB 内存起，20GB 以上可用磁盘；
- Docker Engine 24+ 与 Docker Compose v2；
- 已解析到服务器的域名；
- 防火墙只向公网放行 `80/tcp` 和 `443/tcp`。

先确认 Docker 可用：

```bash
docker version
docker compose version
```

下载仓库：

```bash
git clone https://github.com/dengrb1/ashare-ai-research.git
cd ashare-ai-research
```

## 2. 生成生产配置

```bash
cp .env.production.example .env
cp .env.docker.example .env.docker
chmod 600 .env .env.docker
```

`.env` 保存环境级别和秘密，`.env.docker` 只保存容器内部地址。不要在 `.env.docker` 中再定义 `APP_ENV`，否则会覆盖 `.env` 的 `APP_ENV=production` 并关闭生产安全门禁。旧部署升级时也应删除 `.env.docker` 中的 `APP_ENV=docker`。

可用 URL 安全的随机值作为 PostgreSQL 和 Redis 密码，避免 URL 特殊字符需要额外转义：

```bash
openssl rand -hex 32
openssl rand -hex 32
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

编辑 `.env`，至少替换：

```env
APP_ENV=production
POSTGRES_PASSWORD=<第一个随机值>
REDIS_PASSWORD=<第二个随机值>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<至少 14 位的独立强密码>
COOKIE_SECURE=true
TRUSTED_HOSTS=research.example.com
MODEL_ALLOWED_HOSTS=api.openai.com
MODEL_SETTINGS_ENCRYPTION_KEYS=<Fernet 密钥>
PERSONAL_DATA_ENCRYPTION_KEYS=<URL-safe Base64 编码的 32 字节密钥，可与模型密钥分离轮换>
PRIVATE_OBJECT_ROOT=/data/private
SEARXNG_BASE_URL=http://searxng:8080
CANONICAL_BUNDLE_MODE=akshare
ALLOW_DEMO_DATA=false
```

`TRUSTED_HOSTS` 只写实际域名，不要使用 `*`。`MODEL_ALLOWED_HOSTS` 写 OpenAI-compatible 模型网关的真实主机名，多个主机用逗号分隔。生产模型 URL 必须使用 HTTPS。模型 API Key 可由管理员登录后在“模型设置”中保存，服务端会用 Fernet 密钥加密。

## 3. 启动源码构建栈

始终使用显式项目名，避免因目录名不同启动多套数据卷：

```bash
docker compose -p ashare-ai-src -f compose.yaml config --quiet
docker compose -p ashare-ai-src -f compose.yaml up -d --build
docker compose -p ashare-ai-src -f compose.yaml ps
```

API 启动时会自动执行 Alembic 迁移。首次启动可能需要数分钟拉取镜像和安装依赖。所有服务应最终显示 `healthy`。

验收配置和日志：

```bash
docker compose -p ashare-ai-src -f compose.yaml exec api ashare-ai doctor
docker compose -p ashare-ai-src -f compose.yaml logs --tail 100 api job-worker
```

## 4. SearXNG 联网方式

Compose 中的 `searxng` 服务只声明 `expose: 8080`，没有宿主机端口映射。API 和 Worker 通过 Docker DNS 访问：

```text
http://searxng:8080
```

验收私有搜索服务：

```bash
docker compose -p ashare-ai-src -f compose.yaml ps searxng
docker compose -p ashare-ai-src -f compose.yaml exec -T api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://searxng:8080/healthz', timeout=5).status)"
```

返回 `200` 即表示应用可访问服务器内的 SearXNG。不要把 SearXNG 改为家中电脑的地址，也不要为它增加公网 `ports`。AI 问答只会向该服务发送搜索词，不会把数据库凭据或任意内网访问权交给模型。

## 5. 可选低内存 HTTPS 边缘网关

默认 Compose 只在 `127.0.0.1:80` 暴露 Web，API、PostgreSQL 和 Redis 也只绑定本机。若希望由本仓库提供 HTTPS 终止层，可启用独立的 `edge-gateway` profile；不启用时，原有 Caddy、Nginx 或云负载均衡器方案保持不变。

启用前，完成下列前提：

- 边缘网关域名（`edge_domain` / `EDGE_DOMAIN`）的 DNS A/AAAA 记录已指向此服务器；
- 准备好用于 ACME 账号恢复通知的邮箱（`edge_acme_email` / `EDGE_ACME_EMAIL`）。

网关配置由「管理 → Edge Gateway」专用页面管理：管理员解锁后编辑 FRP TOML，并用结构化代理主机表维护 Nginx。FRP 内容服务端加密保存，控制器只在配置哈希变化时原子写入并重建网关；无需手工编辑 `.env`。首次启用前设置以下环境变量：

管理员也可以直接编辑 `docker/edge-gateway/frpc.toml` 和 `docker/edge-gateway/managed.conf`（这两个文件默认被 Git 忽略）。管理页面和本机控制器会在读取/轮询时检测文件哈希并导入版本；下一次应用会先校验再覆盖，外部修改不会被静默忽略。

```env
EDGE_DOMAIN=
EDGE_ACME_EMAIL=
EDGE_FRPC_ENABLED=false
EDGE_FRPC_CONFIG_FILE=./docker/edge-gateway/frpc.disabled.toml
EDGE_GATEWAY_ENCRYPTION_KEYS=<Fernet key>
EDGE_GATEWAY_CONFIG_DIR=./.secrets/edge-gateway
EDGE_PROXY_TARGET_ALLOWLIST=web
```

首次启动会以 ACME HTTP-01 自动申请 ECDSA P-256 证书，证书和 ACME 账号保存在 Compose 的 `edge-certificates`、`edge-acme-data` 卷中：

```bash
docker compose -p ashare-ai-src -f compose.yaml --profile edge up -d --build
docker compose -p ashare-ai-src -f compose.yaml --profile edge ps edge-gateway
docker compose -p ashare-ai-src -f compose.yaml --profile edge logs --tail 100 edge-gateway
```

该服务只代理到 Compose 内的 `web:80`，不映射宿主机端口；FRP 直接连接同一容器的 `127.0.0.1:80/443`。HTTP 除 ACME challenge 外均跳转 HTTPS；TLS 仅允许 1.2/1.3，未知 Host/SNI 会被拒绝。它会覆盖客户端提交的 `X-Forwarded-For`，使应用既有登录限流和审计不会被伪造来源 IP 绕过。网关不增加第二套登录机制，继续使用应用的 Cookie/Bearer 鉴权。

容器运行单个 Nginx worker，限制为 64 MiB 和 32 个 PID；acme.sh 仅在启动与周期续期时运行。请定期备份两个 edge 卷，且不要用 `docker compose down -v` 删除它们。

### 可选 frpc 客户端

网关不部署 frps。若服务器需要通过已有外部 frps 暴露，复制 `docker/edge-gateway/frpc.toml.example` 到未跟踪的私有路径，填入真实 frps 地址、token 和域名；该配置中的两个代理必须分别指向同容器的 `127.0.0.1:80` 与 `127.0.0.1:443`。外部 frps 必须配置 `vhostHTTPPort=80` 与 `vhostHTTPSPort=443`，以便 HTTP-01 和 TLS 透传均能到达网关。随后在系统设置「公网边缘网关」中开启 `edge_frpc_enabled`，并把 `edge_frpc_config_file` 指向该文件（例如 `./.secrets/edge-frpc.toml`）；拓扑控制器启动网关前会校验该文件存在。

`frpc.toml` 包含 token，绝不能提交、打印或放入 `*.example`。启用 `frpc` 前确认它只能声明你授权暴露的代理。关闭隧道只需在系统设置关闭 `edge_frpc_enabled` 并保存；控制器会重建 `edge-gateway`，不会删除证书卷。

### 本机拓扑控制器（Windows、Linux、macOS）

控制逻辑是跨平台 Python 程序；它只在设置发生变化时调用 Compose。平台适配仅负责每分钟的无界面调度：Windows 使用 `pythonw.exe` 计划任务，Linux 使用 `systemd --user` timer，macOS 使用 LaunchAgent。安装器不会启动容器；系统设置中“公网边缘网关（FRP）”仍默认关闭。

```powershell
# Windows
.\scripts\install-topology-controller.ps1
```

```bash
# Linux/macOS（项目虚拟环境已激活）
python scripts/install_topology_controller.py --install
```

控制器日志保存在未跟踪的 `.secrets/topology-controller.log`。卸载分别使用 `schtasks /Delete /TN AshareAiTopologyController /F`、`systemctl --user disable --now ashare-ai-topology.timer` 或 `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.ashare.topology.plist`。

## 6. 使用外部 TLS 反向代理

不启用 `edge` profile 时，可继续在同机用 Caddy、Nginx 或云负载均衡器终止 TLS。Caddy 最小示例：

```caddyfile
research.example.com {
    reverse_proxy 127.0.0.1:80
}
```

启用 HTTPS 后访问 `https://research.example.com`。不要直接把 `8000`、`5432`、`6379` 或 SearXNG 端口暴露到公网。

## 7. 直接使用 GHCR 镜像

不在服务器编译应用时，使用基础 Compose 加 GHCR 覆盖文件：

```bash
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml pull
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml up -d
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml ps
```

默认拉取本仓库的 `latest` 镜像。Fork 部署可在 `.env` 中设置 `ASHARE_APP_IMAGE`、`ASHARE_WEB_IMAGE` 和 `ASHARE_POSTGRES_IMAGE`。私有 GHCR Package 需先执行 `docker login ghcr.io`。

## 8. 升级

升级前先备份 PostgreSQL：

```bash
mkdir -p backups
docker compose -p ashare-ai-src -f compose.yaml exec -T postgres \
  pg_dump -U ashare -d ashare -Fc > "backups/ashare-$(date +%F-%H%M).dump"
```

源码构建方式：

```bash
git pull --ff-only
docker compose -p ashare-ai-src -f compose.yaml up -d --build
docker compose -p ashare-ai-src -f compose.yaml ps
```

GHCR 方式：

```bash
git pull --ff-only
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml pull
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml up -d
docker compose -p ashare-ai -f compose.yaml -f compose.ghcr.yaml ps
```

不要在升级时执行 `docker compose down -v`；`-v` 会删除 PostgreSQL、Redis、lake 和对象数据卷。数据库迁移为向前演进，如需回退应使用升级前备份，不要只切回旧镜像后继续写入新结构数据库。

## 9. 常用运维命令

```bash
# 服务状态
docker compose -p ashare-ai-src -f compose.yaml ps -a

# API 和 Worker 日志
docker compose -p ashare-ai-src -f compose.yaml logs --tail 200 api job-worker

# 持续跟踪日志
docker compose -p ashare-ai-src -f compose.yaml logs -f api job-worker

# 重启某个应用服务，不删除数据卷
docker compose -p ashare-ai-src -f compose.yaml restart api
```

如 API 日志显示无法解析 `postgres`，或 Worker 无法解析 `redis`，先检查同一项目名下的依赖：

```bash
docker compose -p ashare-ai-src -f compose.yaml ps -a
docker compose ls --all
docker ps -a
```

先恢复 PostgreSQL、Redis 和 SearXNG 并等待健康检查通过，再启动 API/Worker。`Exited (0)` 且日志含 `SIGTERM` 通常表示容器被正常停止，不等同于 OOM。

## 10. 默认 Worker 与并行扩展

默认 `job-worker` 串行处理研究、交易方案和回测，独立的 `exit-advice-worker` 处理卖出建议，避免被长任务阻塞。重任务、退出建议和周期清理都在短生命周期子进程中执行，父进程不会保留其科学计算堆。此时 `research-worker` 属于未启用的 `dual-research` profile，不会创建容器。通过系统设置保存 `DUAL`，确认没有活动研究或回测后，再执行页面返回的命令（或等效命令）：

```bash
docker compose -p ashare-ai-src -f compose.yaml --profile dual-research \
  up -d --force-recreate job-worker research-worker
```

该 profile 固定使用两个 `research-worker` 副本，每个副本串行消费一条研究任务；队列与运行产物按 `run_id` 隔离，因此两个不同研究可同时推进。`job-worker` 也会按保存的 `DUAL` 模式跳过研究队列，避免竞争领取。切回 `SERIAL` 后，按页面命令停止研究 Worker 并重建 `job-worker`。

设置页会按运行环境实时内存、Worker 实测基线和两个 700 MiB Worker 最大预算展示 `NORMAL/WARNING/CRITICAL` 提醒；内存状态本身不拒绝保存 DUAL。两条研究各自最多 4 路 LLM 组件请求，模型网关需支持最多 8 路并发；若网关容量不足，将 `LLM_AGENT_MAX_CONCURRENCY` 降为 `2`，任务级双并发保持不变。切换前仍须确认没有正在运行的研究或回测任务；内存余量紧张时优先使用默认串行 Worker。
