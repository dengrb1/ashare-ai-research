[CmdletBinding()]
param(
    [ValidateSet("Start", "Stop", "Restart", "Status")]
    [string]$Action = "Status",
    [string[]]$Services = @("Stack"),
    [switch]$SkipBuild
)

# Shared lifecycle manager for every background process owned by the project.
# start_stack.ps1 / stop_stack.ps1 delegate here so GUI and CLI always use the
# same PID state file and never accidentally leave an orphaned component behind.

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runDir = Join-Path $repoRoot ".run"
$stackStatePath = Join-Path $runDir "stack.json"
$stdinPath = Join-Path $runDir "service.stdin"
$webDir = Join-Path $repoRoot "web"
$gatewayDir = Join-Path $repoRoot "gateway"
$npmCache = Join-Path $repoRoot ".npm-cache"
$llmScript = Join-Path $PSScriptRoot "llm_stack.ps1"
$settingsPath = Join-Path $repoRoot "config\settings.json"
$bundledPython = Join-Path $repoRoot "runtime\python\python.exe"
$pythonCommand = if (Test-Path $bundledPython) { $bundledPython } else { "python.exe" }
$configuredPorts = @{}
if (Test-Path $settingsPath) {
    try {
        $settings = Get-Content -Raw $settingsPath | ConvertFrom-Json
        $system = $settings.system
        if ($system.gateway_port) { $configuredPorts.gateway = [int]$system.gateway_port }
        if ($system.quote_bridge_port) { $configuredPorts.quote = [int]$system.quote_bridge_port }
        if ($system.control_api_port) { $configuredPorts.api = [int]$system.control_api_port }
    } catch { Write-Warning "Ignoring invalid port settings: $settingsPath" }
}

$serviceDefinitions = @{
    Gateway = [pscustomobject]@{
        StateKey = "gateway_pid"
        Port = $(if ($configuredPorts.gateway) { $configuredPorts.gateway } else { 8787 })
        HealthUrl = "http://127.0.0.1:$($(if ($configuredPorts.gateway) { $configuredPorts.gateway } else { 8787 }))/health/ready"
        LogPrefix = "gateway"
        ProcessNames = @("ashare-model-gateway")
    }
    QuoteBridge = [pscustomobject]@{
        StateKey = "bridge_pid"
        Port = $(if ($configuredPorts.quote) { $configuredPorts.quote } else { 8081 })
        HealthUrl = "http://127.0.0.1:$($(if ($configuredPorts.quote) { $configuredPorts.quote } else { 8081 }))/health"
        LogPrefix = "bridge"
        ProcessNames = @("python", "python3")
    }
    Api = [pscustomobject]@{
        StateKey = "api_pid"
        Port = $(if ($configuredPorts.api) { $configuredPorts.api } else { 8000 })
        HealthUrl = "http://127.0.0.1:$($(if ($configuredPorts.api) { $configuredPorts.api } else { 8000 }))/api/health"
        LogPrefix = "api"
        ProcessNames = @("python", "python3")
    }
}
$stackServices = @("Gateway", "QuoteBridge", "Api")

function Expand-Services {
    param([string[]]$Requested)

    $expanded = [System.Collections.Generic.List[string]]::new()
    foreach ($requestedService in $Requested) {
        foreach ($service in ($requestedService -split ",")) {
            $service = $service.Trim()
            $names = switch ($service) {
                "Stack" { $stackServices }
                "All" { @($stackServices + "LocalModel") }
                "Api" { @($service) }
                "Gateway" { @($service) }
                "QuoteBridge" { @($service) }
                "LocalModel" { @($service) }
                default { throw "Unknown service '$service'. Use Stack, Api, Gateway, QuoteBridge, LocalModel, or All." }
            }
            foreach ($name in $names) {
                if (-not $expanded.Contains($name)) {
                    $expanded.Add($name)
                }
            }
        }
    }
    return @($expanded)
}

function Get-StackState {
    if (-not (Test-Path $stackStatePath)) {
        return [pscustomobject]@{}
    }
    try {
        $state = Get-Content -Raw $stackStatePath | ConvertFrom-Json
        if ($null -ne $state) {
            return $state
        }
    } catch {
        Write-Warning "Ignoring unreadable state file: $stackStatePath ($($_.Exception.Message))"
    }
    return [pscustomobject]@{}
}

function Get-StatePid {
    param(
        [object]$State,
        [string]$StateKey
    )

    $property = $State.PSObject.Properties[$StateKey]
    if ($null -eq $property -or $null -eq $property.Value) {
        return $null
    }
    try {
        return [int]$property.Value
    } catch {
        return $null
    }
}

function Set-StatePid {
    param(
        [object]$State,
        [string]$StateKey,
        [Nullable[int]]$ManagedPid
    )

    if ($null -eq $ManagedPid) {
        $State.PSObject.Properties.Remove($StateKey)
    } else {
        # Nullable[int] is unboxed to Int32 by Windows PowerShell; accessing
        # .Value therefore produces $null and silently drops the PID.
        $State | Add-Member -NotePropertyName $StateKey -NotePropertyValue ([int]$ManagedPid) -Force
    }
}

function Save-StackState {
    param([object]$State)

    $hasProcess = $false
    foreach ($definition in $serviceDefinitions.Values) {
        if (Get-StatePid $State $definition.StateKey) {
            $hasProcess = $true
            break
        }
    }
    if (-not $hasProcess) {
        Remove-Item -LiteralPath $stackStatePath -ErrorAction SilentlyContinue
        return
    }
    $State | Add-Member -NotePropertyName "updated_at" -NotePropertyValue (Get-Date).ToUniversalTime().ToString("o") -Force
    $State | ConvertTo-Json | Set-Content -Encoding UTF8 $stackStatePath
}

function Get-PortListenerPid {
    param([int]$Port)

    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $listener) {
        return [int]$listener.OwningProcess
    }
    return $null
}

function Get-ProcessDetails {
    param([Nullable[int]]$ProcessId)

    if ($null -eq $ProcessId) {
        return $null
    }
    try {
        return Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$ProcessId)" -ErrorAction SilentlyContinue
    } catch {
        return $null
    }
}

function Test-ProjectOwnedProcess {
    param(
        [string]$Service,
        [Nullable[int]]$ProcessId
    )

    $process = Get-ProcessDetails $ProcessId
    if ($null -eq $process) {
        return $false
    }
    $commandLine = [string]$process.CommandLine
    $executablePath = [string]$process.ExecutablePath
    $combined = "$commandLine`n$executablePath".ToLowerInvariant()
    $rootToken = $repoRoot.ToLowerInvariant().TrimEnd('\')

    # A process started outside service_control.ps1 can still be one of this
    # repository's services (for example after a console crash). Reclaim only
    # when its command line is specific to the service, or its executable lives
    # below this repository. Unrelated listeners remain untouched.
    switch ($Service) {
        "Api" {
            return $combined.Contains("a_share_ai_trader.control_api") -or
                $combined.Contains("a_share_ai_trader\\control_api.py")
        }
        "QuoteBridge" {
            return $combined.Contains("tools\\quote_bridge\\server.py") -or
                $combined.Contains("tools/quote_bridge/server.py")
        }
        "Gateway" {
            return ($combined.Contains("ashare-model-gateway") -and $combined.Contains($rootToken)) -or
                ($combined.Contains("\\gateway\\target\\release\\") -and $combined.Contains($rootToken))
        }
    }
    return $false
}

function Stop-ProcessAndWait {
    param(
        [int]$ProcessId,
        [string]$Service
    )

    Stop-Process -Id $ProcessId -ErrorAction Stop
    Wait-Process -Id $ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    if (Test-ProcessRunning $ProcessId) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $ProcessId -Timeout 3 -ErrorAction SilentlyContinue
    }
    if (Test-ProcessRunning $ProcessId) {
        throw "$Service did not stop (pid $ProcessId)."
    }
}

function Test-ProcessRunning {
    param(
        [Nullable[int]]$ManagedPid,
        [string[]]$ExpectedProcessNames = @()
    )

    # Windows PowerShell may still evaluate both sides of an -and expression.
    # Keep the null guard explicit so an empty stack.json never becomes
    # "Cannot bind argument to parameter 'Id' because it is null".
    if ($null -eq $ManagedPid) {
        return $false
    }
    $process = Get-Process -Id ([int]$ManagedPid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    if ($ExpectedProcessNames.Count -gt 0 -and $ExpectedProcessNames -notcontains $process.ProcessName) {
        return $false
    }
    return $true
}

function Test-Health {
    param([string]$Url)

    try {
        return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $Url).StatusCode -eq 200
    } catch {
        return $false
    }
}

function Import-ServiceEnvironment {
    $packageEnvPath = Join-Path $repoRoot "package.env"
    if (Test-Path $packageEnvPath) {
        foreach ($line in (Get-Content -LiteralPath $packageEnvPath)) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$' -and $line -notmatch '^\s*#') {
                Set-Item -Path ("Env:" + $Matches[1]) -Value $Matches[2]
            }
        }
    }
    foreach ($envName in @(
        "OKINTO_API_KEY",
        "LLM_PRIMARY_API_KEY",
        "LLM_SECONDARY_API_KEY",
        "LOCAL_MODEL_API_KEY",
        "GATEWAY_CLIENT_API_KEY"
    )) {
        if (-not [Environment]::GetEnvironmentVariable($envName)) {
            $imported = [Environment]::GetEnvironmentVariable($envName, "User")
            if (-not $imported) {
                $imported = [Environment]::GetEnvironmentVariable($envName, "Machine")
            }
            if ($imported) {
                Set-Item -Path "Env:$envName" -Value $imported
            }
        }
    }
}

function Build-StackAssets {
    if (-not (Test-Path (Join-Path $webDir "node_modules"))) {
        $env:npm_config_cache = $npmCache
        & npm.cmd install --prefix $webDir
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    }
    & npm.cmd run build --prefix $webDir
    if ($LASTEXITCODE -ne 0) { throw "web build failed" }
    & cargo.exe build --release --manifest-path (Join-Path $gatewayDir "Cargo.toml")
    if ($LASTEXITCODE -ne 0) { throw "gateway build failed" }
}

function Start-ManagedService {
    param(
        [string]$Service,
        [object]$State
    )

    $definition = $serviceDefinitions[$Service]
    $knownPid = Get-StatePid $State $definition.StateKey
    if (Test-ProcessRunning $knownPid $definition.ProcessNames) {
        Write-Host "$Service is already running (pid $knownPid)."
        return
    }
    if ($null -ne $knownPid) {
        Set-StatePid $State $definition.StateKey $null
    }

    $listenerPid = Get-PortListenerPid $definition.Port
    if ($null -ne $listenerPid) {
        throw "$Service cannot start: port $($definition.Port) is already in use by unmanaged PID $listenerPid."
    }

    $stdoutPath = Join-Path $runDir "$($definition.LogPrefix).out.log"
    $stderrPath = Join-Path $runDir "$($definition.LogPrefix).err.log"
    $commonArgs = @{
        WorkingDirectory = $repoRoot
        RedirectStandardInput = $stdinPath
        RedirectStandardOutput = $stdoutPath
        RedirectStandardError = $stderrPath
        WindowStyle = "Hidden"
        PassThru = $true
    }

    $process = switch ($Service) {
        "Gateway" {
            $gatewayExe = Join-Path $gatewayDir "target\release\ashare-model-gateway.exe"
            if (-not (Test-Path $gatewayExe)) {
                throw "Gateway binary is missing at $gatewayExe. Start with a full build first."
            }
            Start-Process -FilePath $gatewayExe -WorkingDirectory $gatewayDir `
                -RedirectStandardInput $stdinPath -RedirectStandardOutput $stdoutPath `
                -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
        }
        "QuoteBridge" {
            Start-Process -FilePath $pythonCommand -ArgumentList "tools\quote_bridge\server.py", "--port", "8081" @commonArgs
        }
        "Api" {
            Start-Process -FilePath $pythonCommand -ArgumentList "-m", "a_share_ai_trader.control_api" @commonArgs
        }
    }
    Set-StatePid $State $definition.StateKey ([Nullable[int]]$process.Id)
    Write-Host "$Service started (pid $($process.Id), port $($definition.Port))."
}

function Stop-ManagedService {
    param(
        [string]$Service,
        [object]$State
    )

    $definition = $serviceDefinitions[$Service]
    $managedPid = Get-StatePid $State $definition.StateKey
    if (Test-ProcessRunning $managedPid $definition.ProcessNames) {
        Stop-ProcessAndWait ([int]$managedPid) $Service
        Write-Host "$Service stopped (pid $managedPid)."
    } elseif ($null -ne $managedPid) {
        Write-Host "$Service was already stopped (stale pid $managedPid cleared)."
    } else {
        $listenerPid = Get-PortListenerPid $definition.Port
        if ($null -ne $listenerPid) {
            if (Test-ProjectOwnedProcess $Service $listenerPid) {
                Stop-ProcessAndWait ([int]$listenerPid) $Service
                Write-Host "$Service stopped discovered project process (pid $listenerPid)."
            } else {
                Write-Warning "$Service has no managed PID; leaving external PID $listenerPid on port $($definition.Port) untouched."
            }
        } else {
            Write-Host "$Service is not running."
        }
    }
    Set-StatePid $State $definition.StateKey $null
}

function Wait-ForHealth {
    param([string[]]$TargetServices)

    $managedTargets = @($TargetServices | Where-Object { $_ -ne "LocalModel" })
    if ($managedTargets.Count -eq 0) {
        return
    }
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $unhealthy = @($managedTargets | Where-Object { -not (Test-Health $serviceDefinitions[$_].HealthUrl) })
        if ($unhealthy.Count -eq 0) {
            Write-Host "Requested services are healthy."
            return
        }
        Start-Sleep -Milliseconds 300
    } until ((Get-Date) -gt $deadline)
    throw "Services did not become healthy: $($unhealthy -join ', '). Inspect .run\*.err.log."
}

function Write-ServiceStatus {
    param([string[]]$TargetServices)

    $state = Get-StackState
    $status = foreach ($service in $TargetServices) {
        if ($service -eq "LocalModel") {
            $llmStatePath = Join-Path $runDir "llm.json"
            $managedPid = $null
            if (Test-Path $llmStatePath) {
                try { $managedPid = [int]((Get-Content -Raw $llmStatePath | ConvertFrom-Json).llm_pid) } catch {}
            }
            [pscustomobject]@{
                service = $service
                managed_pid = $managedPid
                process_running = (Test-ProcessRunning $managedPid @("llama-server", "llama"))
                healthy = (Test-Health "http://127.0.0.1:8080/health")
                port = 8080
            }
            continue
        }
        $definition = $serviceDefinitions[$service]
        $managedPid = Get-StatePid $state $definition.StateKey
        [pscustomobject]@{
            service = $service
            managed_pid = $managedPid
            process_running = (Test-ProcessRunning $managedPid $definition.ProcessNames)
            healthy = (Test-Health $definition.HealthUrl)
            port = $definition.Port
        }
    }
    $status | ConvertTo-Json
}

New-Item -ItemType Directory -Force $runDir, $npmCache | Out-Null
if (-not (Test-Path $stdinPath)) {
    New-Item -ItemType File -Path $stdinPath | Out-Null
}
$targets = Expand-Services $Services

if ($Action -eq "Status") {
    Write-ServiceStatus $targets
    return
}

Import-ServiceEnvironment
$state = Get-StackState
if ($Action -eq "Restart") {
    foreach ($service in ($targets | Where-Object { $_ -ne "LocalModel" })) {
        Stop-ManagedService $service $state
    }
    if ($targets -contains "LocalModel") {
        & $llmScript -Stop
    }
    Save-StackState $state
    $Action = "Start"
}

if ($Action -eq "Stop") {
    foreach ($service in ($targets | Where-Object { $_ -ne "LocalModel" })) {
        Stop-ManagedService $service $state
    }
    if ($targets -contains "LocalModel") {
        & $llmScript -Stop
    }
    Save-StackState $state
    return
}

if (-not $SkipBuild -and ($Services -contains "Stack" -or $Services -contains "All")) {
    Build-StackAssets
}
foreach ($service in ($targets | Where-Object { $_ -ne "LocalModel" })) {
    Start-ManagedService $service $state
    Save-StackState $state
}
if ($targets -contains "LocalModel") {
    & $llmScript -Start
}
Wait-ForHealth $targets
