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
        Write-Host "解压并导入镜像包..." -ForegroundColor Cyan
        $imagePath = Join-Path $InstallDir "ashare-ai-images.tar.gz"
        & gzip -dc $imagePath | docker load
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
        $postgresPassword = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 16 | ForEach-Object {[char]$_})
        $redisPassword = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 16 | ForEach-Object {[char]$_})
        $apiSecretKey = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | ForEach-Object {[char]$_})

        @"
# AShare AI 配置文件
POSTGRES_PASSWORD=$postgresPassword
REDIS_PASSWORD=$redisPassword
API_SECRET_KEY=$apiSecretKey

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
