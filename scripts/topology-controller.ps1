[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { $Python = 'python.exe' }
& $Python (Join-Path $PSScriptRoot 'topology_controller.py')
exit $LASTEXITCODE
<#
# Docker Compose prints normal progress lines to stderr.  Treat only its exit
# code as failure so a successful status message cannot turn into a PowerShell
# exception under PowerShell 7.
$PSNativeCommandUseErrorActionPreference = $false
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TokenFile = Join-Path $ProjectRoot '.secrets\topology-controller.token'
$LogFile = Join-Path $ProjectRoot '.secrets\topology-controller.log'
$StateFile = Join-Path $ProjectRoot '.secrets\topology-controller.state'

function Write-ControllerLog([string] $Message) {
    "$(Get-Date -Format o) $Message" | Add-Content -LiteralPath $LogFile -Encoding utf8
}

function Invoke-Compose([string[]] $Arguments) {
    & docker @Arguments 2>&1 | ForEach-Object { Write-ControllerLog $_ }
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

function Stop-ComposeServiceIfRunning([string[]] $Compose, [string] $Profile, [string] $Service) {
    $running = & docker @($Compose + @('--profile', $Profile, 'ps', '--status', 'running', '-q', $Service)) 2>$null
    if ($LASTEXITCODE -ne 0) { throw "cannot inspect $Service (exit code $LASTEXITCODE)" }
    if ($running) { Invoke-Compose ($Compose + @('--profile', $Profile, 'stop', $Service)) }
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
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null
    if (-not (Test-Path -LiteralPath $TokenFile)) { throw 'topology controller token file is missing' }
    $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    if ($token.Length -lt 32) { throw 'topology controller token is invalid' }
    $desired = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8000/api/internal/topology-desired' -Headers @{ 'X-Topology-Controller-Token' = $token } -TimeoutSec 10
    $desiredState = "$($desired.research_execution_mode):$($desired.edge_gateway_enabled)"
    if ((Test-Path -LiteralPath $StateFile) -and ((Get-Content -LiteralPath $StateFile -Raw).Trim() -eq $desiredState)) {
        exit 0
    }
    $compose = @('compose', '-p', 'ashare-ai-src', '-f', 'compose.yaml')
    Push-Location $ProjectRoot
    try {
        if ($desired.research_execution_mode -eq 'DUAL') {
            Invoke-Compose ($compose + @('--profile', 'dual-research', 'up', '-d', '--no-build', 'job-worker', 'research-worker'))
        } else {
            Stop-ComposeServiceIfRunning $compose 'dual-research' 'research-worker'
            Invoke-Compose ($compose + @('up', '-d', '--no-build', 'job-worker'))
        }
        if ($desired.edge_gateway_enabled -eq $true) {
            Test-EdgeConfiguration
            Invoke-Compose ($compose + @('--profile', 'edge', 'up', '-d', '--no-build', 'edge-gateway'))
        } else {
            Stop-ComposeServiceIfRunning $compose 'edge' 'edge-gateway'
        }
        Set-Content -LiteralPath $StateFile -Value $desiredState -NoNewline -Encoding ascii
    } finally {
        Pop-Location
    }
} catch {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null
    Write-ControllerLog "ERROR $($_.Exception.Message)"
    exit 1
}
#>
