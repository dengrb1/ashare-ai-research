[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TokenDirectory = Join-Path $ProjectRoot '.secrets'
$TokenFile = Join-Path $TokenDirectory 'topology-controller.token'
$EnvFile = Join-Path $ProjectRoot '.env'

New-Item -ItemType Directory -Force -Path $TokenDirectory | Out-Null
if (-not (Test-Path -LiteralPath $TokenFile)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [Convert]::ToBase64String($bytes) | Set-Content -LiteralPath $TokenFile -NoNewline -Encoding ascii
}
$token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
if (-not (Test-Path -LiteralPath $EnvFile)) { New-Item -ItemType File -Path $EnvFile | Out-Null }
$lines = Get-Content -LiteralPath $EnvFile
$lines = @($lines | Where-Object { $_ -notmatch '^TOPOLOGY_CONTROLLER_TOKEN=' })
$lines += "TOPOLOGY_CONTROLLER_TOKEN=$token"
foreach ($default in @(
    'EDGE_DOMAIN=ashare.dengrb.top',
    'EDGE_FRPC_ENABLED=true',
    'EDGE_FRPC_CONFIG_FILE=./.secrets/edge-frpc.toml'
)) {
    $key = $default.Split('=', 2)[0]
    if (-not ($lines | Where-Object { $_ -match "^$key=" })) { $lines += $default }
}
Set-Content -LiteralPath $EnvFile -Value $lines -Encoding utf8

$script = Join-Path $PSScriptRoot 'topology-controller.ps1'
$taskCommand = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`""
& schtasks.exe /Create /TN 'AshareAiTopologyController' /TR $taskCommand /SC MINUTE /MO 1 /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'failed to create the AshareAiTopologyController scheduled task' }
& schtasks.exe /Run /TN 'AshareAiTopologyController' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'failed to run the AshareAiTopologyController scheduled task' }
Write-Host 'Installed AshareAiTopologyController. It runs once per minute and has no persistent Docker container.'
