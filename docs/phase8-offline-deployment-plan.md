# Phase 8: 离线部署包

## 目标

打包完整的离线安装包，使系统可以在无互联网环境下一键部署和运行。

## 前置条件

- 已完成 Phase 1-7
- 系统功能完整且稳定
- Docker 镜像构建正常
- 所有服务可独立运行

## 使用场景

1. **内网部署**: 企业内部网络，无外网访问
2. **私有云**: 私有服务器，不依赖公有云
3. **演示环境**: 客户现场演示，无网络依赖
4. **备份恢复**: 离线备份，快速恢复部署

## Phase 8A: 需求分析

### 离线包应包含的内容

1. **Docker 镜像**
   - API 服务镜像
   - Web 服务镜像
   - Gateway 镜像
   - Bridge 服务镜像
   - PostgreSQL 镜像
   - Redis 镜像
   - 可选: SearXNG 镜像（如果启用搜索）

2. **配置文件**
   - `compose.yaml`
   - `.env.example`
   - 初始化脚本

3. **数据库初始化**
   - Schema 迁移脚本
   - 初始数据（可选）

4. **文档**
   - 安装指南
   - 配置说明
   - 故障排除

5. **依赖工具**
   - Docker Engine 安装包（Windows/Linux）
   - Docker Compose 二进制
   - 验证脚本

### 离线包不包含的内容

- 源代码（可选）
- 开发工具（node, rust, python）
- 历史数据（用户自行导入）

## Phase 8B: 镜像打包

### 实施步骤

1. **构建所有镜像**
   ```bash
   # 在联网环境构建
   docker compose build --no-cache
   
   # 验证镜像列表
   docker images | grep ashare
   ```

2. **导出镜像为 tar**
   ```bash
   # 创建镜像列表
   cat > images.txt <<EOF
   ashare-ai-src-api:latest
   ashare-ai-src-web:latest
   ashare-ai-src-gateway:latest
   ashare-ai-src-quote-bridge:latest
   ashare-ai-src-news-bridge:latest
   postgres:17-alpine
   redis:7-alpine
   nginx:1.27-alpine
   EOF
   
   # 导出所有镜像
   docker save $(cat images.txt) -o ashare-ai-images.tar
   
   # 压缩（可选，但推荐）
   gzip ashare-ai-images.tar
   # 输出: ashare-ai-images.tar.gz (~500MB - 2GB)
   ```

3. **验证导出**
   ```bash
   # 检查 tar 文件内容
   tar -tzf ashare-ai-images.tar.gz | head -20
   ```

## Phase 8C: 部署脚本

### 8C.1 Windows 部署脚本

**文件**: `scripts/offline-install.ps1`

```powershell
#!/usr/bin/env pwsh
# AShare AI 离线安装脚本 - Windows

param(
    [string]$InstallDir = "C:\AshareAI",
    [switch]$SkipDockerCheck,
    [switch]$ImportImages
)

$ErrorActionPreference = "Stop"

Write-Host "=== AShare AI 离线安装 ===" -ForegroundColor Cyan

# 1. 检查 Docker
if (-not $SkipDockerCheck) {
    Write-Host "`n[1/6] 检查 Docker..." -ForegroundColor Yellow
    
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Host "错误: 未检测到 Docker，请先安装 Docker Desktop" -ForegroundColor Red
        Write-Host "下载地址: https://www.docker.com/products/docker-desktop/" -ForegroundColor Cyan
        exit 1
    }
    
    try {
        docker version | Out-Null
    } catch {
        Write-Host "错误: Docker 未运行，请启动 Docker Desktop" -ForegroundColor Red
        exit 1
    }
    
    Write-Host "✓ Docker 已就绪" -ForegroundColor Green
}

# 2. 创建安装目录
Write-Host "`n[2/6] 创建安装目录..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Set-Location $InstallDir
Write-Host "✓ 安装目录: $InstallDir" -ForegroundColor Green

# 3. 解压安装包
Write-Host "`n[3/6] 解压安装包..." -ForegroundColor Yellow
if (Test-Path ".\ashare-ai-offline.zip") {
    Expand-Archive -Path ".\ashare-ai-offline.zip" -DestinationPath "." -Force
    Write-Host "✓ 安装包已解压" -ForegroundColor Green
} else {
    Write-Host "警告: 未找到 ashare-ai-offline.zip，跳过解压" -ForegroundColor Yellow
}

# 4. 导入 Docker 镜像
if ($ImportImages) {
    Write-Host "`n[4/6] 导入 Docker 镜像..." -ForegroundColor Yellow
    
    if (Test-Path ".\ashare-ai-images.tar.gz") {
        Write-Host "解压镜像包..." -ForegroundColor Cyan
        & 7z x ashare-ai-images.tar.gz -so | docker load
        Write-Host "✓ 镜像导入完成" -ForegroundColor Green
    } elseif (Test-Path ".\ashare-ai-images.tar") {
        docker load -i ashare-ai-images.tar
        Write-Host "✓ 镜像导入完成" -ForegroundColor Green
    } else {
        Write-Host "警告: 未找到镜像包，跳过导入" -ForegroundColor Yellow
    }
} else {
    Write-Host "`n[4/6] 跳过镜像导入 (使用 -ImportImages 启用)" -ForegroundColor Yellow
}

# 5. 生成配置文件
Write-Host "`n[5/6] 生成配置文件..." -ForegroundColor Yellow

if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item .env.example .env
    } else {
        # 生成默认配置
        @"
# AShare AI 配置文件
POSTGRES_PASSWORD=$(New-Guid).Guid
REDIS_PASSWORD=$(New-Guid).Guid
API_SECRET_KEY=$(New-Guid).Guid

# 绑定地址（127.0.0.1 仅本机访问，0.0.0.0 允许网络访问）
API_BIND_ADDRESS=127.0.0.1
WEB_BIND_ADDRESS=127.0.0.1
SERVICE_BIND_ADDRESS=127.0.0.1

# 日志级别
LOG_LEVEL=info
GATEWAY_LOG_LEVEL=info
"@ | Out-File -FilePath .env -Encoding UTF8
    }
    Write-Host "✓ 配置文件已生成: .env" -ForegroundColor Green
    Write-Host "  请根据需要编辑配置" -ForegroundColor Cyan
} else {
    Write-Host "✓ 配置文件已存在: .env" -ForegroundColor Green
}

# 6. 启动服务
Write-Host "`n[6/6] 启动服务..." -ForegroundColor Yellow
docker compose up -d

Write-Host "`n=== 安装完成 ===" -ForegroundColor Green
Write-Host "`n服务地址:" -ForegroundColor Cyan
Write-Host "  Web UI:  http://localhost" -ForegroundColor White
Write-Host "  API:     http://localhost:8000" -ForegroundColor White
Write-Host "  Gateway: http://localhost:8787" -ForegroundColor White
Write-Host "`n健康检查:" -ForegroundColor Cyan
Write-Host "  docker compose ps" -ForegroundColor White
Write-Host "  curl http://localhost:8000/api/v1/health" -ForegroundColor White
Write-Host "`n日志查看:" -ForegroundColor Cyan
Write-Host "  docker compose logs -f" -ForegroundColor White
Write-Host "`n停止服务:" -ForegroundColor Cyan
Write-Host "  docker compose down" -ForegroundColor White
```

### 8C.2 Linux 部署脚本

**文件**: `scripts/offline-install.sh`

```bash
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
        echo "解压镜像包..."
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
```

### 8C.3 卸载脚本

**文件**: `scripts/offline-uninstall.sh`

```bash
#!/usr/bin/env bash
# AShare AI 卸载脚本

set -euo pipefail

echo "=== AShare AI 卸载 ==="
echo "警告: 这将删除所有容器、数据卷和镜像"
read -p "确认继续？(yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "取消卸载"
    exit 0
fi

# 停止并删除容器
echo -e "\n[1/4] 停止服务..."
docker compose down -v

# 删除镜像
echo -e "\n[2/4] 删除镜像..."
docker images | grep ashare | awk '{print $1":"$2}' | xargs -r docker rmi

# 删除数据卷（可选）
echo -e "\n[3/4] 删除数据卷..."
docker volume ls | grep ashare | awk '{print $2}' | xargs -r docker volume rm

# 删除安装目录（可选）
echo -e "\n[4/4] 删除安装目录..."
read -p "是否删除安装目录？(yes/no): " delete_dir
if [ "$delete_dir" = "yes" ]; then
    sudo rm -rf "$PWD"
    echo "✓ 安装目录已删除"
else
    echo "保留安装目录"
fi

echo -e "\n=== 卸载完成 ==="
```

## Phase 8D: 打包流程

### 实施步骤

1. **创建打包脚本**

**文件**: `scripts/build-offline-package.sh`

```bash
#!/usr/bin/env bash
# 构建离线安装包

set -euo pipefail

PACKAGE_NAME="ashare-ai-offline-$(date +%Y%m%d)"
PACKAGE_DIR="dist/$PACKAGE_NAME"

echo "=== 构建离线安装包 ==="
echo "包名: $PACKAGE_NAME"

# 1. 创建打包目录
echo -e "\n[1/7] 创建打包目录..."
rm -rf "dist/$PACKAGE_NAME"
mkdir -p "$PACKAGE_DIR"

# 2. 构建 Docker 镜像
echo -e "\n[2/7] 构建 Docker 镜像..."
docker compose build --no-cache

# 3. 导出镜像
echo -e "\n[3/7] 导出 Docker 镜像..."
cat > /tmp/images.txt <<EOF
ashare-ai-src-api:latest
ashare-ai-src-web:latest
ashare-ai-src-gateway:latest
ashare-ai-src-quote-bridge:latest
ashare-ai-src-news-bridge:latest
postgres:17-alpine
redis:7-alpine
EOF

docker save $(cat /tmp/images.txt) | gzip > "$PACKAGE_DIR/ashare-ai-images.tar.gz"
echo "✓ 镜像包大小: $(du -h $PACKAGE_DIR/ashare-ai-images.tar.gz | cut -f1)"

# 4. 复制配置文件
echo -e "\n[4/7] 复制配置文件..."
cp compose.yaml "$PACKAGE_DIR/"
cp .env.example "$PACKAGE_DIR/"
cp .dockerignore "$PACKAGE_DIR/" 2>/dev/null || true

# 5. 复制脚本
echo -e "\n[5/7] 复制脚本..."
mkdir -p "$PACKAGE_DIR/scripts"
cp scripts/offline-install.sh "$PACKAGE_DIR/"
cp scripts/offline-install.ps1 "$PACKAGE_DIR/"
cp scripts/offline-uninstall.sh "$PACKAGE_DIR/"
chmod +x "$PACKAGE_DIR"/*.sh

# 6. 复制文档
echo -e "\n[6/7] 复制文档..."
mkdir -p "$PACKAGE_DIR/docs"
cp README.md "$PACKAGE_DIR/"
cp docs/DOCKER_DEPLOY.md "$PACKAGE_DIR/docs/" 2>/dev/null || true

# 生成安装指南
cat > "$PACKAGE_DIR/INSTALL.md" <<'EOF'
# AShare AI 离线安装指南

## 系统要求

- **操作系统**: Windows 10+, Ubuntu 20.04+, CentOS 7+
- **Docker**: 20.10+
- **内存**: 最低 4GB，推荐 8GB+
- **磁盘**: 最低 10GB 可用空间

## 安装步骤

### Windows

1. 确保 Docker Desktop 已安装并运行
2. 解压安装包
3. 以管理员身份运行 PowerShell
4. 执行安装脚本:
   ```powershell
   .\offline-install.ps1 -ImportImages
   ```

### Linux

1. 确保 Docker 已安装并运行
2. 解压安装包
3. 执行安装脚本:
   ```bash
   sudo bash offline-install.sh
   ```

## 验证安装

访问 http://localhost 查看 Web UI

检查健康状态:
```bash
curl http://localhost:8000/api/v1/health
```

## 故障排除

### 镜像导入失败

```bash
# 手动导入
gunzip -c ashare-ai-images.tar.gz | docker load
```

### 服务启动失败

```bash
# 查看日志
docker compose logs

# 重启服务
docker compose restart
```

### 端口冲突

编辑 `.env` 文件，修改端口配置:
```
API_BIND_ADDRESS=127.0.0.1:8001
WEB_BIND_ADDRESS=127.0.0.1:8080
```

## 卸载

```bash
bash offline-uninstall.sh
```
EOF

# 7. 生成 checksums
echo -e "\n[7/7] 生成校验文件..."
cd "$PACKAGE_DIR"
find . -type f -exec sha256sum {} \; > SHA256SUMS
cd - > /dev/null

# 8. 打包
echo -e "\n[8/8] 压缩打包..."
cd dist
tar -czf "$PACKAGE_NAME.tar.gz" "$PACKAGE_NAME"
cd - > /dev/null

echo -e "\n=== 打包完成 ==="
echo "包路径: dist/$PACKAGE_NAME.tar.gz"
echo "包大小: $(du -h dist/$PACKAGE_NAME.tar.gz | cut -f1)"
echo -e "\nSHA256:"
sha256sum "dist/$PACKAGE_NAME.tar.gz"
```

2. **执行打包**
   ```bash
   bash scripts/build-offline-package.sh
   ```

3. **验证打包**
   ```bash
   # 解压测试
   mkdir -p /tmp/test-install
   cd /tmp/test-install
   tar -xzf /path/to/ashare-ai-offline-YYYYMMDD.tar.gz
   cd ashare-ai-offline-YYYYMMDD
   
   # 检查文件完整性
   sha256sum -c SHA256SUMS
   
   # 测试安装
   bash offline-install.sh
   ```

## Phase 8E: 文档完善

### 安装文档清单

1. **INSTALL.md** - 安装指南
   - 系统要求
   - 安装步骤
   - 验证方法
   - 故障排除

2. **UPGRADE.md** - 升级指南
   - 备份数据
   - 升级步骤
   - 回滚方法

3. **BACKUP.md** - 备份指南
   - 数据备份
   - 配置备份
   - 恢复步骤

4. **FAQ.md** - 常见问题
   - 安装问题
   - 运行问题
   - 性能问题

## 交付物清单

### 离线安装包内容

```
ashare-ai-offline-YYYYMMDD/
├── INSTALL.md                      # 安装指南
├── README.md                       # 项目说明
├── SHA256SUMS                      # 校验文件
├── ashare-ai-images.tar.gz         # Docker 镜像包 (500MB - 2GB)
├── compose.yaml                    # Docker Compose 配置
├── .env.example                    # 配置模板
├── offline-install.sh              # Linux 安装脚本
├── offline-install.ps1             # Windows 安装脚本
├── offline-uninstall.sh            # 卸载脚本
├── docs/
│   ├── DOCKER_DEPLOY.md            # Docker 部署文档
│   ├── UPGRADE.md                  # 升级指南
│   ├── BACKUP.md                   # 备份指南
│   └── FAQ.md                      # 常见问题
└── scripts/
    └── (其他辅助脚本)
```

### 源代码仓库新增文件

- `scripts/build-offline-package.sh` - 打包脚本
- `scripts/offline-install.sh` - Linux 安装脚本
- `scripts/offline-install.ps1` - Windows 安装脚本
- `scripts/offline-uninstall.sh` - 卸载脚本
- `docs/INSTALL.md` - 安装指南
- `docs/UPGRADE.md` - 升级指南
- `docs/BACKUP.md` - 备份指南
- `docs/FAQ.md` - 常见问题
- `docs/phase8-offline-deployment-plan.md` (本文件)
- `docs/fusion-progress.md` - 标记 Phase 8 完成

## 验收标准

### 打包验收

- [ ] 所有必需镜像已导出
- [ ] 镜像包可以成功导入
- [ ] 配置文件完整
- [ ] 脚本有执行权限
- [ ] 文档齐全
- [ ] SHA256 校验通过
- [ ] 包大小合理 (< 3GB)

### 安装验收

- [ ] Windows 脚本安装成功
- [ ] Linux 脚本安装成功
- [ ] 所有服务启动正常
- [ ] 健康检查通过
- [ ] Web UI 可访问
- [ ] API 端点可访问
- [ ] 无外网依赖

### 文档验收

- [ ] 安装指南清晰易懂
- [ ] 故障排除覆盖常见问题
- [ ] 升级指南完整
- [ ] 备份恢复步骤正确

## 使用流程示例

### 构建离线包（开发者，联网环境）

```bash
# 1. 进入项目目录
cd /path/to/ashare-ai-src

# 2. 构建离线包
bash scripts/build-offline-package.sh

# 3. 获取离线包
ls -lh dist/ashare-ai-offline-*.tar.gz

# 4. 传输到离线环境（U盘、内网传输等）
```

### 安装部署（用户，离线环境）

```bash
# 1. 解压安装包
tar -xzf ashare-ai-offline-20260826.tar.gz
cd ashare-ai-offline-20260826

# 2. 验证完整性
sha256sum -c SHA256SUMS

# 3. 执行安装
sudo bash offline-install.sh

# 4. 验证安装
curl http://localhost:8000/api/v1/health

# 5. 访问 Web UI
# 浏览器打开 http://localhost
```

## 风险和注意事项

### 技术风险

1. **镜像体积过大**
   - 风险: 离线包 > 5GB，传输不便
   - 缓解: 优化镜像大小，使用多阶段构建

2. **版本兼容性**
   - 风险: Docker 版本不兼容
   - 缓解: 文档明确最低版本要求

3. **平台差异**
   - 风险: Windows/Linux 脚本行为不一致
   - 缓解: 充分测试两个平台

### 业务风险

1. **数据迁移**
   - 风险: 旧版本数据无法迁移
   - 缓解: 提供数据迁移工具和文档

2. **配置错误**
   - 风险: 用户配置错误导致服务无法启动
   - 缓解: 提供配置验证脚本

## 实施步骤顺序

1. **Phase 8A: 需求分析**（1 小时）
   - 确定离线包内容
   - 确定目标平台

2. **Phase 8B: 镜像打包**（2 小时）
   - 构建镜像
   - 导出压缩
   - 验证导入

3. **Phase 8C: 部署脚本**（4-6 小时）
   - Windows 脚本
   - Linux 脚本
   - 卸载脚本
   - 测试验证

4. **Phase 8D: 打包流程**（2-3 小时）
   - 打包脚本
   - 校验生成
   - 端到端测试

5. **Phase 8E: 文档完善**（3-4 小时）
   - 安装指南
   - 故障排除
   - FAQ

**预计总时间**: 12-16 小时

## 优化建议

### 镜像体积优化

1. **多阶段构建**
   - 已实现: Gateway, Bridge
   - 确保: API 镜像也使用多阶段

2. **精简基础镜像**
   - 使用 alpine 而非 debian
   - 删除不必要的包

3. **分层优化**
   - 依赖层与代码层分离
   - 利用 Docker 缓存

### 部署体验优化

1. **一键脚本**
   - 自动检测环境
   - 智能配置生成
   - 友好的错误提示

2. **健康检查**
   - 安装后自动验证
   - 提供诊断工具

3. **配置向导**
   - 交互式配置生成
   - 配置验证

## 后续维护

### 版本管理

1. **语义化版本**
   - 主版本: 不兼容变更
   - 次版本: 新增功能
   - 修订版本: Bug 修复

2. **发布节奏**
   - 稳定版: 每季度
   - 补丁版: 按需发布

### 升级策略

1. **滚动升级**
   - 零停机升级
   - 蓝绿部署

2. **数据迁移**
   - 自动 schema 迁移
   - 数据备份提醒

## 总结

Phase 8 完成后，整个融合项目（Phase 1-8）全部完成：

- ✅ Phase 1: 研究只读模式闭锁
- ✅ Phase 2: QMT 运行底座迁移
- ✅ Phase 3: Compose 和生命周期统一
- ✅ Phase 4: 数据管道集成
- ✅ Phase 5: 模型训练集成（可选）
- ✅ Phase 6: Rust 优化（可选）
- ✅ Phase 7: Web/PWA 增强（可选）
- ✅ Phase 8: 离线部署包

系统达到生产就绪状态，可以在任意环境独立部署和运行。

---

更新时间：2026-08-26
状态：规划完成，待实施
