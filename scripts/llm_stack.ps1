[CmdletBinding()]
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$packageEnvPath = Join-Path $repoRoot "package.env"
if (Test-Path $packageEnvPath) {
    foreach ($line in (Get-Content -LiteralPath $packageEnvPath)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$' -and $line -notmatch '^\s*#') {
            Set-Item -Path ("Env:" + $Matches[1]) -Value $Matches[2]
        }
    }
}
$runDir = Join-Path $repoRoot ".run"
$statePath = Join-Path $runDir "llm.json"
$llamaDir = Join-Path $repoRoot "tools\llama.cpp"
$serverExe = Join-Path $llamaDir "llama-server.exe"
$modelPath = Join-Path $repoRoot "models\gguf\qwen2.5-3b-instruct-q4_k_m.gguf"
$stdinPath = Join-Path $runDir "service.stdin"
$modelPort = 8080
$settingsPath = Join-Path $repoRoot "config\settings.json"
if (Test-Path $settingsPath) { try { $rawSettings = Get-Content -Raw $settingsPath | ConvertFrom-Json; if ($rawSettings.system.local_model_port) { $modelPort = [int]$rawSettings.system.local_model_port } } catch {} }
if ($env:LOCAL_MODEL_PORT) { try { $modelPort = [int]$env:LOCAL_MODEL_PORT } catch {} }
if ($env:LOCAL_MODEL_PATH) {
    $modelPath = if ([IO.Path]::IsPathRooted($env:LOCAL_MODEL_PATH)) { $env:LOCAL_MODEL_PATH } else { Join-Path $repoRoot $env:LOCAL_MODEL_PATH }
}
$modelLabel = [IO.Path]::GetFileName($modelPath)

New-Item -ItemType Directory -Force $runDir | Out-Null
if (-not (Test-Path $stdinPath)) {
    New-Item -ItemType File -Path $stdinPath | Out-Null
}

function Get-LlmState {
    if (Test-Path $statePath) {
        return Get-Content -Raw $statePath | ConvertFrom-Json
    }
    return $null
}

function Test-PortFree([int]$port) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return -not $listener
}

if ($Status) {
    $state = Get-LlmState
    $running = $false
    if ($state -and $state.llm_pid) {
        $running = [bool](Get-Process -Id $state.llm_pid -ErrorAction SilentlyContinue)
    }
    $healthy = $false
    if ($running) {
        try {
            $healthy = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$modelPort/health").StatusCode -eq 200
        } catch {
            $healthy = $false
        }
    }
    Write-Host ("llama-server: " + $(if ($running) { "running (pid $($state.llm_pid))" } else { "stopped" }))
    Write-Host "OpenAI endpoint: http://127.0.0.1:$modelPort/v1  (model: $modelLabel)"
    Write-Host "Health: $healthy"
    return
}

if ($Stop) {
    $state = Get-LlmState
    if ($state -and $state.llm_pid -and (Get-Process -Id $state.llm_pid -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $state.llm_pid
        Wait-Process -Id $state.llm_pid -Timeout 15 -ErrorAction SilentlyContinue
        Write-Host "llama-server stopped."
    } else {
        Write-Host "llama-server is not running."
    }
    Remove-Item -LiteralPath $statePath -ErrorAction SilentlyContinue
    return
}

if ($Start) {
    $current = Get-LlmState
    if ($current -and $current.llm_pid -and (Get-Process -Id $current.llm_pid -ErrorAction SilentlyContinue)) {
        Write-Host "llama-server is already running (pid $($current.llm_pid))."
        return
    }
    if (-not (Test-PortFree $modelPort)) {
        throw "Port $modelPort is in use. Run .\scripts\llm_stack.ps1 -Stop first, or check the process."
    }
    if (-not (Test-Path $serverExe)) {
        throw "llama-server.exe not found at $serverExe. Extract the win-cuda zip into tools\llama.cpp first."
    }
    if (-not (Test-Path $modelPath)) {
        throw "Model not found at $modelPath. Run the packaged installer or set LOCAL_MODEL_PATH."
    }
    $proc = Start-Process -FilePath $serverExe `
        -ArgumentList "-m", "`"$modelPath`"", "--host", "127.0.0.1", "--port", "$modelPort", `
            "-ngl", "99", "-c", "4096", "--parallel", "1", "-t", "4", "--no-webui" `
        -WorkingDirectory $llamaDir `
        -RedirectStandardInput $stdinPath `
        -RedirectStandardOutput (Join-Path $runDir "llm.out.log") `
        -RedirectStandardError (Join-Path $runDir "llm.err.log") `
        -WindowStyle Hidden -PassThru
    @{
        llm_pid = $proc.Id
        started_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $statePath
    Write-Host "llama-server starting (pid $($proc.Id)); model load takes 10-60s on first start."
    Write-Host "Check progress: Get-Content .run\llm.err.log -Tail 20"
    return
}

Write-Host "Usage: .\scripts\llm_stack.ps1 -Start | -Stop | -Status"
