#!/usr/bin/env bash
# AShare AI 打包脚本 - 创建离线部署包

set -euo pipefail

VERSION="${VERSION:-2.1.1}"
OUTPUT_DIR="${OUTPUT_DIR:-./dist}"
INCLUDE_SOURCE="${INCLUDE_SOURCE:-false}"

echo "=== AShare AI 离线部署包打包 ==="
echo "版本: $VERSION"
echo "输出目录: $OUTPUT_DIR"

# 1. 创建输出目录
echo -e "\n[1/5] 创建输出目录..."
mkdir -p "$OUTPUT_DIR"
PACKAGE_DIR="$OUTPUT_DIR/ashare-ai-$VERSION"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"
echo "✓ 输出目录: $PACKAGE_DIR"

# 2. 复制部署文件
echo -e "\n[2/5] 复制部署文件..."
cp compose.yaml "$PACKAGE_DIR/"
cp .env.example "$PACKAGE_DIR/"
cp README.md "$PACKAGE_DIR/"

# 复制脚本
mkdir -p "$PACKAGE_DIR/scripts"
cp scripts/offline-install.sh "$PACKAGE_DIR/scripts/"
cp scripts/offline-install.ps1 "$PACKAGE_DIR/scripts/"
chmod +x "$PACKAGE_DIR/scripts/"*.sh

# 复制文档
mkdir -p "$PACKAGE_DIR/docs"
cp docs/DEPLOYMENT.md "$PACKAGE_DIR/docs/" 2>/dev/null || echo "跳过 DEPLOYMENT.md"
cp docs/README.md "$PACKAGE_DIR/docs/" 2>/dev/null || echo "跳过 docs/README.md"

echo "✓ 部署文件已复制"

# 3. 构建并导出 Docker 镜像
echo -e "\n[3/5] 构建并导出 Docker 镜像..."
echo "这可能需要几分钟..."

# 构建镜像
docker compose build --no-cache

# 获取镜像列表
IMAGES=$(docker compose config --images | sort -u)
echo "准备导出以下镜像:"
echo "$IMAGES" | sed 's/^/  - /'

# 导出镜像
IMAGES_TAR="$PACKAGE_DIR/ashare-ai-images.tar"
echo "导出镜像到: $IMAGES_TAR"
docker save $IMAGES -o "$IMAGES_TAR"

# 压缩镜像包
echo "压缩镜像包..."
gzip -f "$IMAGES_TAR"
IMAGES_SIZE=$(du -h "$IMAGES_TAR.gz" | cut -f1)
echo "✓ 镜像包已导出: ashare-ai-images.tar.gz ($IMAGES_SIZE)"

# 4. 复制源代码（可选）
if [ "$INCLUDE_SOURCE" = "true" ]; then
    echo -e "\n[4/5] 复制源代码..."
    mkdir -p "$PACKAGE_DIR/src"

    # 复制 Python 后端
    cp -r src/ashare_ai "$PACKAGE_DIR/src/"

    # 复制 Web 前端
    mkdir -p "$PACKAGE_DIR/web"
    cp -r web/src "$PACKAGE_DIR/web/"
    cp -r web/public "$PACKAGE_DIR/web/"
    cp web/package.json "$PACKAGE_DIR/web/"
    cp web/vite.config.ts "$PACKAGE_DIR/web/"
    cp web/tsconfig.json "$PACKAGE_DIR/web/"

    # 复制 Rust Gateway
    mkdir -p "$PACKAGE_DIR/gateway"
    cp -r gateway/src "$PACKAGE_DIR/gateway/"
    cp gateway/Cargo.toml "$PACKAGE_DIR/gateway/"

    echo "✓ 源代码已复制"
else
    echo -e "\n[4/5] 跳过源代码复制 (设置 INCLUDE_SOURCE=true 启用)"
fi

# 5. 创建安装指南
echo -e "\n[5/5] 创建安装指南..."
cat > "$PACKAGE_DIR/INSTALL.md" <<'EOF'
# AShare AI 离线安装指南

## 系统要求

- **操作系统**: Windows 10/11 或 Linux (Ubuntu 20.04+, CentOS 7+)
- **Docker**: Docker Engine 20.10+ 或 Docker Desktop 4.0+
- **内存**: 最低 4GB，推荐 8GB
- **磁盘空间**: 最低 10GB 可用空间
- **CPU**: x86_64 架构

## Windows 安装步骤

### 1. 安装 Docker Desktop

如果尚未安装 Docker Desktop，请从官网下载：
https://www.docker.com/products/docker-desktop/

安装后启动 Docker Desktop。

### 2. 运行安装脚本

打开 PowerShell（以管理员身份），导航到解压目录：

```powershell
cd C:\path\to\ashare-ai-2.1.1
.\scripts\offline-install.ps1 -ImportImages
```

参数说明：
- `-ImportImages`: 导入离线镜像包
- `-InstallDir <路径>`: 自定义安装目录（默认 C:\AshareAI）
- `-SkipDockerCheck`: 跳过 Docker 检查

### 3. 访问服务

安装完成后，浏览器访问：
- Web UI: http://localhost
- API: http://localhost:8000

默认账号：
- 用户名: admin
- 密码: admin123（首次登录后请修改）

## Linux 安装步骤

### 1. 安装 Docker

如果尚未安装 Docker：

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# CentOS/RHEL
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
```

### 2. 运行安装脚本

```bash
cd /path/to/ashare-ai-2.1.1
chmod +x scripts/offline-install.sh
sudo ./scripts/offline-install.sh
```

环境变量：
- `INSTALL_DIR`: 安装目录（默认 /opt/ashare-ai）
- `IMPORT_IMAGES`: 是否导入镜像（默认 true）
- `SKIP_DOCKER_CHECK`: 跳过 Docker 检查（默认 false）

### 3. 访问服务

浏览器访问 http://localhost

## 配置说明

安装脚本会自动生成 `.env` 配置文件，包含随机密码。

如需自定义配置，编辑 `.env` 文件：

```bash
# 数据库密码
POSTGRES_PASSWORD=your_secure_password
REDIS_PASSWORD=your_redis_password

# API 密钥
API_SECRET_KEY=your_secret_key

# 绑定地址
# 127.0.0.1 = 仅本机访问
# 0.0.0.0 = 允许网络访问
API_BIND_ADDRESS=127.0.0.1
WEB_BIND_ADDRESS=127.0.0.1

# 日志级别（debug/info/warning/error）
LOG_LEVEL=info
```

修改配置后重启服务：

```bash
docker compose down
docker compose up -d
```

## 常用命令

### 查看服务状态

```bash
docker compose ps
```

### 查看日志

```bash
# 所有服务
docker compose logs -f

# 单个服务
docker compose logs -f api
docker compose logs -f web
```

### 停止服务

```bash
docker compose down
```

### 启动服务

```bash
docker compose up -d
```

### 备份数据

```bash
# 备份 PostgreSQL
docker compose exec postgres pg_dump -U ashare_user ashare_db > backup.sql

# 备份 Redis
docker compose exec redis redis-cli --rdb dump.rdb
```

### 恢复数据

```bash
# 恢复 PostgreSQL
docker compose exec -T postgres psql -U ashare_user ashare_db < backup.sql
```

## 健康检查

访问以下端点检查服务健康状态：

```bash
# API 健康检查
curl http://localhost:8000/api/v1/health

# Gateway 健康检查
curl http://localhost:8787/health
```

预期响应：
```json
{
  "status": "ok",
  "quote_bridge": {"status": "ok"},
  "news_bridge": {"status": "ok"},
  "gateway": {"status": "ok"}
}
```

## 故障排除

### Docker 未运行

**Windows**: 启动 Docker Desktop
**Linux**: `sudo systemctl start docker`

### 端口冲突

如果 80、8000 或 8787 端口被占用，修改 `compose.yaml` 中的端口映射：

```yaml
services:
  web:
    ports:
      - "8080:80"  # 改为 8080
```

### 容器启动失败

查看日志排查问题：

```bash
docker compose logs <service-name>
```

### 数据库连接失败

检查 PostgreSQL 是否正常启动：

```bash
docker compose ps postgres
docker compose logs postgres
```

### 内存不足

增加 Docker 内存限制：
- **Windows/Mac**: Docker Desktop → Settings → Resources → Memory
- **Linux**: 修改 `/etc/docker/daemon.json`

## 卸载

### Windows

```powershell
cd C:\AshareAI
docker compose down -v
cd ..
Remove-Item -Recurse -Force AshareAI
```

### Linux

```bash
cd /opt/ashare-ai
sudo docker compose down -v
cd ..
sudo rm -rf /opt/ashare-ai
```

`-v` 参数会删除数据卷，请确认已备份重要数据。

## 技术支持

- 文档: docs/
- 问题反馈: https://github.com/your-org/ashare-ai/issues
EOF

echo "✓ 安装指南已创建: INSTALL.md"

# 6. 创建压缩包
echo -e "\n[6/6] 创建压缩包..."
cd "$OUTPUT_DIR"
tar -czf "ashare-ai-$VERSION-offline.tar.gz" "ashare-ai-$VERSION"
PACKAGE_SIZE=$(du -h "ashare-ai-$VERSION-offline.tar.gz" | cut -f1)

echo -e "\n=== 打包完成 ==="
echo "离线部署包: $OUTPUT_DIR/ashare-ai-$VERSION-offline.tar.gz ($PACKAGE_SIZE)"
echo -e "\n包含内容:"
echo "  - Docker 镜像 (10 个服务)"
echo "  - compose.yaml"
echo "  - 安装脚本 (Windows + Linux)"
echo "  - 安装指南 (INSTALL.md)"
echo "  - 示例配置 (.env.example)"
if [ "$INCLUDE_SOURCE" = "true" ]; then
    echo "  - 源代码"
fi
echo -e "\n分发方法:"
echo "  1. 将压缩包复制到目标机器"
echo "  2. 解压: tar -xzf ashare-ai-$VERSION-offline.tar.gz"
echo "  3. 运行安装脚本"
