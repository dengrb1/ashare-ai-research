# AShare AI 离线部署包文档

## 概述

本离线部署包包含 AShare AI 系统的所有必要组件，支持在无互联网环境下一键部署。

## 包含内容

### 1. Docker 镜像

所有服务的预构建 Docker 镜像（约 1-2 GB 压缩后）：

- `ashare-ai-src-api` - Python FastAPI 后端服务
- `ashare-ai-src-web` - React 前端应用
- `ashare-ai-src-gateway` - Rust 模型代理服务
- `ashare-ai-src-quote-bridge` - 行情数据桥接服务
- `ashare-ai-src-news-bridge` - 新闻数据桥接服务
- `ashare-ai-src-job-worker` - 研究任务 Worker
- `ashare-ai-src-exit-advice-worker` - 退出建议 Worker
- `postgres:17-alpine` - PostgreSQL 数据库
- `redis:7-alpine` - Redis 缓存
- `nginx:1.27-alpine` - Nginx 反向代理

### 2. 部署文件

- `compose.yaml` - Docker Compose 编排配置
- `.env.example` - 环境变量模板
- `README.md` - 项目说明

### 3. 安装脚本

- `scripts/offline-install.sh` - Linux 自动安装脚本
- `scripts/offline-install.ps1` - Windows 自动安装脚本
- `scripts/build-offline-package.sh` - 打包脚本（仅用于创建离线包）

### 4. 文档

- `INSTALL.md` - 详细安装指南
- `docs/DEPLOYMENT.md` - 部署配置说明（如果存在）

## 系统要求

### 最低配置
- **CPU**: 2 核心 x86_64
- **内存**: 4 GB RAM
- **磁盘**: 10 GB 可用空间
- **操作系统**: 
  - Windows 10/11 (Docker Desktop)
  - Ubuntu 20.04+
  - CentOS 7+
  - Debian 10+
  - 其他支持 Docker 的 Linux 发行版

### 推荐配置
- **CPU**: 4 核心或更多
- **内存**: 8 GB RAM 或更多
- **磁盘**: 20 GB 可用空间（含数据存储）
- **SSD**: 推荐使用 SSD 提升性能

### 必备软件
- **Docker Engine**: 20.10+ 或 **Docker Desktop**: 4.0+
- **Docker Compose**: 2.0+ (通常随 Docker 安装)

## 快速开始

### Windows

1. 确保 Docker Desktop 已安装并运行
2. 解压离线部署包到目标目录
3. 以管理员身份打开 PowerShell
4. 运行安装脚本：

```powershell
cd C:\path\to\ashare-ai-2.2.0
.\scripts\offline-install.ps1 -ImportImages
```

5. 等待安装完成（约 5-10 分钟）
6. 浏览器访问 http://localhost

### Linux

1. 确保 Docker 已安装并运行
2. 解压离线部署包：

```bash
tar -xzf ashare-ai-2.2.0-offline.tar.gz
cd ashare-ai-2.2.0
```

3. 运行安装脚本：

```bash
chmod +x scripts/offline-install.sh
sudo ./scripts/offline-install.sh
```

4. 等待安装完成
5. 浏览器访问 http://localhost

## 安装选项

### Windows PowerShell 参数

```powershell
.\scripts\offline-install.ps1 [-ImportImages] [-InstallDir <路径>] [-SkipDockerCheck]
```

- `-ImportImages`: 导入离线镜像包（首次安装必须）
- `-InstallDir`: 自定义安装目录，默认 `C:\AshareAI`
- `-SkipDockerCheck`: 跳过 Docker 环境检查

### Linux 环境变量

```bash
INSTALL_DIR=/custom/path IMPORT_IMAGES=true ./scripts/offline-install.sh
```

- `INSTALL_DIR`: 安装目录，默认 `/opt/ashare-ai`
- `IMPORT_IMAGES`: 是否导入镜像，默认 `true`
- `SKIP_DOCKER_CHECK`: 跳过 Docker 检查，默认 `false`

## 配置说明

安装脚本会自动生成 `.env` 配置文件，包含随机密码。

### 核心配置项

```bash
# 数据库密码（自动生成）
POSTGRES_PASSWORD=<随机密码>
REDIS_PASSWORD=<随机密码>

# API 密钥（自动生成）
API_SECRET_KEY=<随机密钥>

# 服务绑定地址
# 127.0.0.1 = 仅本机访问（默认，安全）
# 0.0.0.0 = 允许网络访问（需要时修改）
API_BIND_ADDRESS=127.0.0.1
WEB_BIND_ADDRESS=127.0.0.1
SERVICE_BIND_ADDRESS=127.0.0.1

# 日志级别
LOG_LEVEL=info
GATEWAY_LOG_LEVEL=info
```

### 允许网络访问

如需从其他机器访问，修改 `.env`：

```bash
WEB_BIND_ADDRESS=0.0.0.0
API_BIND_ADDRESS=0.0.0.0
```

然后重启服务：

```bash
docker compose down
docker compose up -d
```

### 自定义端口

如果默认端口被占用，修改 `compose.yaml` 中的端口映射：

```yaml
services:
  web:
    ports:
      - "8080:80"  # 改为 8080 端口
  
  api:
    ports:
      - "8001:8000"  # 改为 8001 端口
```

## 服务管理

### 查看服务状态

```bash
docker compose ps
```

预期输出：
```
NAME                           STATUS    PORTS
ashare-ai-api                  Up        0.0.0.0:8000->8000/tcp
ashare-ai-web                  Up        0.0.0.0:80->80/tcp
ashare-ai-gateway              Up        0.0.0.0:8787->8787/tcp
ashare-ai-quote-bridge         Up        8081/tcp
ashare-ai-news-bridge          Up        8082/tcp
ashare-ai-job-worker           Up        
ashare-ai-exit-advice-worker   Up        
ashare-ai-postgres             Up        5432/tcp
ashare-ai-redis                Up        6379/tcp
```

### 查看日志

```bash
# 所有服务
docker compose logs -f

# 单个服务
docker compose logs -f api
docker compose logs -f web
docker compose logs -f gateway

# 最近 100 行
docker compose logs --tail=100
```

### 重启服务

```bash
# 重启所有服务
docker compose restart

# 重启单个服务
docker compose restart api
```

### 停止服务

```bash
docker compose down
```

### 启动服务

```bash
docker compose up -d
```

## 健康检查

### API 健康检查

```bash
curl http://localhost:8000/api/v1/health
```

预期响应：
```json
{
  "status": "ok",
  "timestamp": "2026-08-26T10:00:00Z",
  "quote_bridge": {"status": "ok", "url": "http://quote-bridge:8081"},
  "news_bridge": {"status": "ok", "url": "http://news-bridge:8082"},
  "gateway": {"status": "ok", "url": "http://gateway:8787"}
}
```

### Gateway 健康检查

```bash
curl http://localhost:8787/health
```

### Web UI 访问

浏览器访问 http://localhost，应看到登录页面。

默认账号：
- 用户名: `admin`
- 密码: `admin123`

**安全提示**: 首次登录后请立即修改密码。

## 数据备份与恢复

### 备份数据库

```bash
# 进入安装目录
cd /opt/ashare-ai  # Linux
cd C:\AshareAI     # Windows

# 备份 PostgreSQL
docker compose exec postgres pg_dump -U ashare_user ashare_db > backup_$(date +%Y%m%d).sql

# 备份 Redis (RDB 快照)
docker compose exec redis redis-cli BGSAVE
```

### 恢复数据库

```bash
# 恢复 PostgreSQL
docker compose exec -T postgres psql -U ashare_user ashare_db < backup_20260826.sql
```

### 备份数据卷

```bash
# 完整备份所有数据卷
docker compose down
tar -czf ashare-ai-volumes-backup.tar.gz \
  /var/lib/docker/volumes/ashare-ai-src_postgres-data \
  /var/lib/docker/volumes/ashare-ai-src_redis-data
docker compose up -d
```

## 故障排除

### Docker 未运行

**症状**: `Cannot connect to the Docker daemon`

**解决方案**:
- Windows: 启动 Docker Desktop
- Linux: `sudo systemctl start docker`

### 端口冲突

**症状**: `Bind for 0.0.0.0:80 failed: port is already allocated`

**解决方案**: 修改 `compose.yaml` 中的端口映射，参见"自定义端口"章节。

### 容器启动失败

**症状**: 某个服务状态为 `Exited` 或 `Restarting`

**解决方案**:
1. 查看日志: `docker compose logs <service-name>`
2. 检查配置: 确认 `.env` 文件格式正确
3. 检查资源: `docker stats` 查看内存/CPU 使用情况

### 数据库连接失败

**症状**: API 日志显示 `psycopg2.OperationalError`

**解决方案**:
1. 检查 PostgreSQL 状态: `docker compose ps postgres`
2. 查看 PostgreSQL 日志: `docker compose logs postgres`
3. 确认密码配置: `.env` 中 `POSTGRES_PASSWORD` 与 `compose.yaml` 一致

### 内存不足

**症状**: 容器被 OOM Killer 杀死

**解决方案**:
- Docker Desktop: Settings → Resources → Memory (建议至少 4GB)
- Linux: 检查 `free -h` 输出，考虑添加 swap 或增加物理内存

### 镜像导入失败

**症状**: `error loading image: layer does not exist`

**解决方案**:
1. 验证镜像包完整性: `gzip -t ashare-ai-images.tar.gz`
2. 检查磁盘空间: `df -h`
3. 手动导入: `gunzip -c ashare-ai-images.tar.gz | docker load`

## 升级

### 小版本升级 (2.1.1 → 2.2.0)

1. 停止服务: `docker compose down`
2. 备份数据（参见"数据备份"）
3. 替换新版本的 `compose.yaml` 和镜像
4. 导入新镜像: 重新运行安装脚本的镜像导入步骤
5. 启动服务: `docker compose up -d`

### 大版本升级 (2.x → 3.x)

参考具体版本的升级文档，可能需要数据库迁移。

## 卸载

### 保留配置和数据

```bash
docker compose down
```

### 完全卸载（删除所有数据）

**警告**: 此操作不可恢复，请先备份重要数据。

**Windows**:
```powershell
cd C:\AshareAI
docker compose down -v
cd ..
Remove-Item -Recurse -Force AshareAI
```

**Linux**:
```bash
cd /opt/ashare-ai
sudo docker compose down -v
cd ..
sudo rm -rf /opt/ashare-ai
```

`-v` 参数会删除所有数据卷（数据库、缓存、日志）。

## 性能调优

### PostgreSQL 优化

编辑 `compose.yaml`，在 `postgres` 服务中添加：

```yaml
services:
  postgres:
    command:
      - postgres
      - -c
      - shared_buffers=256MB
      - -c
      - effective_cache_size=1GB
      - -c
      - max_connections=100
```

### Redis 优化

对于高负载场景，启用 AOF 持久化：

```yaml
services:
  redis:
    command: redis-server --appendonly yes --appendfsync everysec
```

### Worker 并发

增加 Worker 数量处理更多任务：

```bash
docker compose up -d --scale job-worker=3
```

## 安全建议

1. **修改默认密码**: 首次登录后立即修改 admin 密码
2. **限制网络访问**: 生产环境使用 `127.0.0.1` 绑定，通过反向代理暴露服务
3. **HTTPS**: 在反向代理层配置 TLS/SSL 证书
4. **防火墙**: 仅开放必要的端口（80/443）
5. **定期备份**: 建立自动备份计划
6. **日志监控**: 定期检查日志，及时发现异常
7. **版本更新**: 关注安全更新，及时升级

## 技术支持

- **文档**: 查看 `docs/` 目录下的详细文档
- **日志**: 通过 `docker compose logs` 获取运行日志
- **问题反馈**: GitHub Issues 或联系技术支持团队

## 附录

### 服务端口映射

| 服务 | 内部端口 | 外部端口 | 说明 |
|---|---|---|---|
| Web | 80 | 80 | 前端应用 |
| API | 8000 | 8000 | 后端 API |
| Gateway | 8787 | 8787 | 模型代理 |
| Quote Bridge | 8081 | - | 内部服务 |
| News Bridge | 8082 | - | 内部服务 |
| PostgreSQL | 5432 | - | 内部服务 |
| Redis | 6379 | - | 内部服务 |

### 容器资源限制

当前配置的资源限制（参见 `compose.yaml`）：

| 服务 | 内存限制 | CPU 限制 |
|---|---|---|
| API | 384 MB | - |
| Job Worker | 700 MB | - |
| Exit Advice Worker | 320 MB | - |
| Web | 32 MB | - |
| Gateway | 128 MB | - |
| Quote Bridge | 64 MB | - |
| News Bridge | 64 MB | - |
| PostgreSQL | 128 MB | - |
| Redis | 64 MB | - |

根据实际负载可能需要调整这些限制。

---

**版本**: 2.2.0
**更新时间**: 2026-08-26  
**适用平台**: Windows 10/11, Linux (Ubuntu/CentOS/Debian)
