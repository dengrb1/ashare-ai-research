[CmdletBinding()]
param(
    [int]$ApiPort = 18000,
    [int]$GatewayPort = 18787
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runDir = Join-Path $repoRoot ".run"
$stdinPath = Join-Path $runDir "service.stdin"
$gatewayExe = Join-Path $repoRoot "gateway\target\release\ashare-model-gateway.exe"
$apiUrl = "http://127.0.0.1:$ApiPort"
$gatewayUrl = "http://127.0.0.1:$GatewayPort"

$env:CONTROL_API_PORT = "$ApiPort"
$env:GATEWAY_LISTEN = "127.0.0.1:$GatewayPort"
$env:MODEL_GATEWAY_URL = $gatewayUrl
$env:ASHARE_CONSOLE_URL = $apiUrl

& (Join-Path $PSScriptRoot "stop_stack.ps1")
if (-not (Test-Path $stdinPath)) {
    New-Item -ItemType File -Path $stdinPath | Out-Null
}
$occupied = @(
    Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
    Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue
) | Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::", "::1") }
if ($occupied) {
    $owners = ($occupied | Select-Object -ExpandProperty OwningProcess -Unique) -join ","
    throw "verification ports are already in use by PID(s): $owners"
}

$gateway = $null
$api = $null
try {
    $gateway = Start-Process -FilePath $gatewayExe `
        -WorkingDirectory (Join-Path $repoRoot "gateway") `
        -RedirectStandardInput $stdinPath `
        -RedirectStandardOutput (Join-Path $runDir "gateway.verify.out.log") `
        -RedirectStandardError (Join-Path $runDir "gateway.verify.err.log") `
        -WindowStyle Hidden -PassThru
    $api = Start-Process -FilePath "python.exe" `
        -ArgumentList "-m", "a_share_ai_trader.control_api" `
        -WorkingDirectory $repoRoot `
        -RedirectStandardInput $stdinPath `
        -RedirectStandardOutput (Join-Path $runDir "api.verify.out.log") `
        -RedirectStandardError (Join-Path $runDir "api.verify.err.log") `
        -WindowStyle Hidden -PassThru

    $deadline = (Get-Date).AddSeconds(30)
    $ready = $false
    do {
        Start-Sleep -Milliseconds 300
        try {
            if ($gateway.HasExited -or $api.HasExited) {
                throw "verification child process exited"
            }
            $apiReady = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "$apiUrl/api/health").StatusCode -eq 200
            $gatewayReady = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "$gatewayUrl/health/ready").StatusCode -eq 200
            $ready = $apiReady -and $gatewayReady
        } catch {
            $ready = $false
        }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) {
        throw "verification stack did not become healthy"
    }
    try {
        $listeners = @(
            Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction Stop
            Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction Stop
        )
        $listenerPids = @($listeners | Select-Object -ExpandProperty OwningProcess)
        if ($listenerPids.Count -gt 0 -and ($listenerPids -notcontains $api.Id -or $listenerPids -notcontains $gateway.Id)) {
            throw "health endpoints are served by unexpected process IDs"
        }
    } catch {
        Write-Warning "Could not inspect listener ownership; child process liveness was verified instead: $($_.Exception.Message)"
    }

    Write-Host "--- health ---"
    (Invoke-WebRequest -UseBasicParsing "$apiUrl/api/health").Content

    Write-Host "--- gateway fallback ---"
    $chat = Invoke-WebRequest -UseBasicParsing -Method POST -ContentType "application/json" `
        -Body '{"model":"ashare-advisor","messages":[{"role":"user","content":"health"}]}' `
        "$gatewayUrl/v1/chat/completions"
    "provider=$($chat.Headers["x-gateway-provider"]) status=$($chat.StatusCode)"
    $chat.Content

    Write-Host "--- control-plane probe ---"
    (Invoke-WebRequest -UseBasicParsing -Method POST -ContentType "application/json" `
        -Body '{"prompt":"status"}' "$apiUrl/api/gateway/probe").Content

    Write-Host "--- order guard ---"
    $order = Invoke-WebRequest -UseBasicParsing -Method POST -ContentType "application/json" `
        -Body '{"symbol":"600690.SH","side":"BUY","limit_price":20,"reason":"verification"}' `
        "$apiUrl/api/orders"
    "status=$($order.StatusCode)"
    $order.Content

    Write-Host "--- gateway benchmark ---"
    python scripts\gateway_benchmark.py --url $gatewayUrl --requests 500 --concurrency 16
    if ($LASTEXITCODE -ne 0) { throw "gateway benchmark failed" }
    $gatewayProcess = Get-Process -Id $gateway.Id -ErrorAction SilentlyContinue
    if ($gatewayProcess) {
        "gateway_rss_bytes=$($gatewayProcess.WorkingSet64)"
    } else {
        "gateway_rss_bytes=unavailable_process_exited"
    }

    Write-Host "--- browser visual check ---"
    node scripts\visual_check.mjs
    if ($LASTEXITCODE -ne 0) { throw "browser visual check failed" }
} finally {
    foreach ($process in @($api, $gateway)) {
        if ($null -ne $process -and (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
