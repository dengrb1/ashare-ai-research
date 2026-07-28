[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TokenFile = Join-Path $ProjectRoot '.secrets\topology-controller.token'
$LogFile = Join-Path $ProjectRoot '.secrets\topology-controller.log'

function Write-ControllerLog([string] $Message) {
    "$(Get-Date -Format o) $Message" | Add-Content -LiteralPath $LogFile -Encoding utf8
}

function Invoke-Compose([string[]] $Arguments) {
    & docker @Arguments 2>&1 | ForEach-Object { Write-ControllerLog $_ }
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

function Test-EdgeConfiguration {
    $envFile = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { throw 'edge-gateway requires a local .env file' }
    $values = @{}
    Get-Content -LiteralPath $envFile | ForEach-Object {
        if ($_ -match '^([^#=]+)=(.*)$') { $values[$matches[1]] = $matches[2] }
    }
    foreach ($required in 'EDGE_DOMAIN', 'EDGE_ACME_EMAIL', 'EDGE_FRPC_CONFIG_FILE') {
        if (-not $values.ContainsKey($required) -or [string]::IsNullOrWhiteSpace($values[$required])) {
            throw "edge-gateway requires $required in .env"
        }
    }
    $frpcConfig = $values['EDGE_FRPC_CONFIG_FILE']
    if (-not [System.IO.Path]::IsPathRooted($frpcConfig)) { $frpcConfig = Join-Path $ProjectRoot $frpcConfig }
    if (-not (Test-Path -LiteralPath $frpcConfig)) { throw 'edge-gateway frpc config file is missing' }
}

try {
    if (-not (Test-Path -LiteralPath $TokenFile)) { throw 'topology controller token file is missing' }
    $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    if ($token.Length -lt 32) { throw 'topology controller token is invalid' }
    $desired = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8000/api/internal/topology-desired' -Headers @{ 'X-Topology-Controller-Token' = $token } -TimeoutSec 10
    $compose = @('compose', '-p', 'ashare-ai-src', '-f', 'compose.yaml')
    Push-Location $ProjectRoot
    try {
        if ($desired.research_execution_mode -eq 'DUAL') {
            Invoke-Compose ($compose + @('--profile', 'dual-research', 'up', '-d', '--no-build', 'job-worker', 'research-worker'))
        } else {
            & docker @($compose + @('--profile', 'dual-research', 'stop', 'research-worker')) *> $null
            Invoke-Compose ($compose + @('up', '-d', '--no-build', 'job-worker'))
        }
        if ($desired.edge_gateway_enabled -eq $true) {
            Test-EdgeConfiguration
            Invoke-Compose ($compose + @('--profile', 'edge', 'up', '-d', '--no-build', 'edge-gateway'))
        } else {
            & docker @($compose + @('--profile', 'edge', 'stop', 'edge-gateway')) *> $null
        }
    } finally {
        Pop-Location
    }
} catch {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null
    Write-ControllerLog "ERROR $($_.Exception.Message)"
    exit 1
}
