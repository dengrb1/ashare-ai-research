#!/usr/bin/env bash
# AShare AI 离线安装脚本 - Linux

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/ashare-ai}"
SKIP_DOCKER_CHECK="${SKIP_DOCKER_CHECK:-false}"
IMPORT_IMAGES="${IMPORT_IMAGES:-true}"

echo "=== AShare AI 离线安装 ==="

# 1. 检查 Docker
if [ "$SKIP_DOCKER_CHECK" != "true" ]; then
    echo -e "\n[1/6] 检查 Docker..."

    if ! command -v docker &> /dev/null; then
        echo "错误: 未检测到 Docker"
        echo "安装方法: https://docs.docker.com/engine/install/"
        exit 1
    fi

    if ! docker version &> /dev/null; then
        echo "错误: Docker 未运行"
        echo "启动方法: sudo systemctl start docker"
        exit 1
    fi

    echo "✓ Docker 已就绪"
fi

# 2. 创建安装目录
echo -e "\n[2/6] 创建安装目录..."
sudo mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
echo "✓ 安装目录: $INSTALL_DIR"

# 3. 解压安装包
echo -e "\n[3/6] 解压安装包..."
if [ -f "ashare-ai-offline.tar.gz" ]; then
    sudo tar -xzf ashare-ai-offline.tar.gz
    echo "✓ 安装包已解压"
else
    echo "警告: 未找到 ashare-ai-offline.tar.gz，跳过解压"
fi

# 4. 导入 Docker 镜像
if [ "$IMPORT_IMAGES" = "true" ]; then
    echo -e "\n[4/6] 导入 Docker 镜像..."

    if [ -f "ashare-ai-images.tar.gz" ]; then
        echo "解压并导入镜像包..."
        gunzip -c ashare-ai-images.tar.gz | docker load
        echo "✓ 镜像导入完成"
    elif [ -f "ashare-ai-images.tar" ]; then
        docker load -i ashare-ai-images.tar
        echo "✓ 镜像导入完成"
    else
        echo "警告: 未找到镜像包，跳过导入"
    fi
else
    echo -e "\n[4/6] 跳过镜像导入 (设置 IMPORT_IMAGES=true 启用)"
fi

# 5. 生成配置文件
echo -e "\n[5/6] 生成配置文件..."

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
    else
        # 生成默认配置
        cat > .env <<EOF
# AShare AI 配置文件
POSTGRES_PASSWORD=$(openssl rand -hex 16)
REDIS_PASSWORD=$(openssl rand -hex 16)
API_SECRET_KEY=$(openssl rand -hex 32)

# 绑定地址（127.0.0.1 仅本机访问，0.0.0.0 允许网络访问）
API_BIND_ADDRESS=127.0.0.1
WEB_BIND_ADDRESS=127.0.0.1
SERVICE_BIND_ADDRESS=127.0.0.1

# 日志级别
LOG_LEVEL=info
GATEWAY_LOG_LEVEL=info
EOF
    fi
    echo "✓ 配置文件已生成: .env"
    echo "  请根据需要编辑配置"
else
    echo "✓ 配置文件已存在: .env"
fi

# 6. 启动服务
echo -e "\n[6/6] 启动服务..."
docker compose up -d

echo -e "\n=== 安装完成 ==="
echo -e "\n服务地址:"
echo "  Web UI:  http://localhost"
echo "  API:     http://localhost:8000"
echo "  Gateway: http://localhost:8787"
echo -e "\n健康检查:"
echo "  docker compose ps"
echo "  curl http://localhost:8000/api/v1/health"
echo -e "\n日志查看:"
echo "  docker compose logs -f"
echo -e "\n停止服务:"
echo "  docker compose down"
