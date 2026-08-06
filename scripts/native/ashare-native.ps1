[CmdletBinding()]
param(
    [ValidateSet("install", "start", "stop", "restart", "repair", "status", "doctor", "watchdog")]
    [string]$Command = "status",
    [string]$Root,
    [string]$SourceRoot,
    [ValidateSet("SERIAL", "DUAL")]
    [string]$ResearchMode = "SERIAL",
    [ValidateRange(0, 2)]
    [int]$ResearchWorkers = 0,
    [ValidateRange(5, 300)]
    [int]$WatchdogIntervalSeconds = 10,
    [switch]$Json,
    [switch]$Fast,
    [switch]$NoWatchdog,
    [string]$AdminUsername = "admin",
    [string]$AdminPassword
)

$ErrorActionPreference = "Stop"
$script:NativeVersion = "2026.08.06.3"
$script:PostgresPort = 55432
$script:RedisPort = 56379
$script:ApiPort = 58000
$script:SearxngPort = 58080
$script:ScriptRoot = (Resolve-Path $PSScriptRoot).Path
$script:SourceRoot = if ($SourceRoot) {
    (Resolve-Path $SourceRoot).Path
} else {
    (Resolve-Path (Join-Path $script:ScriptRoot "..\..")).Path
}
$script:Root = if ($Root) {
    [IO.Path]::GetFullPath($Root)
} elseif ($env:ASHARE_NATIVE_ROOT) {
    [IO.Path]::GetFullPath($env:ASHARE_NATIVE_ROOT)
} else {
    Join-Path $env:LOCALAPPDATA "AshareAI\runtime"
}
$script:StatePath = Join-Path $script:Root "state\processes.json"
$script:DesiredStatePath = Join-Path $script:Root "state\desired-state.json"
$script:WatchdogStatePath = Join-Path $script:Root "state\watchdog.json"
$script:TaskNamePath = Join-Path $script:Root "state\watchdog-task.txt"
$script:EnvPath = Join-Path $script:Root ".env"
$script:PortsPath = Join-Path $script:Root "config\native-ports.json"
$script:ManifestPath = Join-Path $script:ScriptRoot "dependencies.lock.json"

function Get-NativeRuntimeId {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($script:Root.TrimEnd("\").ToLowerInvariant())
        return (([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace "-", "").ToLowerInvariant()).Substring(0, 16)
    } finally {
        $sha.Dispose()
    }
}

$script:RuntimeId = Get-NativeRuntimeId
$script:ManagementMutexName = "Global\AshareAI-Native-Management-$($script:RuntimeId)"
$script:WatchdogMutexName = "Global\AshareAI-Native-Watchdog-$($script:RuntimeId)"
$script:WatchdogTaskName = "AshareAI Native Watchdog $($script:RuntimeId)"
$script:WatchdogLogPath = Join-Path $script:Root "logs\watchdog.log"

function Write-AtomicText([string]$path, [string]$content) {
    $directory = Split-Path -Parent $path
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $temporary = "$path.$PID.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        # UTF-8 without BOM: PowerShell 5.1 Set-Content -Encoding UTF8 prepends a BOM,
        # which breaks consumers like Redis's config parser or python-dotenv on the
        # first line (the BOM becomes part of the first key/directive).
        [System.IO.File]::WriteAllText($temporary, [string]$content, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-NativeEvent([string]$message, [string]$level = "INFO") {
    $logPath = $script:WatchdogLogPath
    $logDirectory = Split-Path -Parent $logPath
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        $length = (Get-Item -LiteralPath $logPath).Length
        if ($length -gt 5MB) {
            $previous = "$logPath.1"
            if (Test-Path -LiteralPath $previous) {
                Remove-Item -LiteralPath $previous -Force -ErrorAction SilentlyContinue
            }
            Move-Item -LiteralPath $logPath -Destination $previous -Force
        }
    }
    $line = "{0} [{1}] {2}" -f [DateTime]::UtcNow.ToString("o"), $level, $message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function New-NativeMutex([string]$name) {
    return [Threading.Mutex]::new($false, $name)
}

function Enter-NativeManagementLock {
    $mutex = New-NativeMutex $script:ManagementMutexName
    try {
        try {
            if (-not $mutex.WaitOne(0)) {
                $mutex.Dispose()
                throw "another native management operation is already running"
            }
        } catch [Threading.AbandonedMutexException] {
            # The previous manager exited while holding the lock; this caller owns it now.
        }
        return $mutex
    } catch {
        if ($mutex) { $mutex.Dispose() }
        throw
    }
}

function Exit-NativeMutex($mutex) {
    if ($null -eq $mutex) { return }
    try { $mutex.ReleaseMutex() } catch { }
    $mutex.Dispose()
}

function Set-NativeDesiredState([ValidateSet("RUNNING", "STOPPED")][string]$state) {
    $payload = [ordered]@{
        desired_state = $state
        runtime_id = $script:RuntimeId
        updated_at = [DateTime]::UtcNow.ToString("o")
    }
    Write-AtomicText $script:DesiredStatePath ($payload | ConvertTo-Json -Depth 5)
}

function Get-NativeDesiredState {
    if (-not (Test-Path -LiteralPath $script:DesiredStatePath -PathType Leaf)) {
        return "STOPPED"
    }
    try {
        $payload = Get-Content -Raw -LiteralPath $script:DesiredStatePath | ConvertFrom-Json
        if ($payload.desired_state -in @("RUNNING", "STOPPED")) { return [string]$payload.desired_state }
    } catch { }
    return "STOPPED"
}

function Write-NativeWatchdogState([hashtable]$values) {
    $payload = [ordered]@{
        runtime_id = $script:RuntimeId
        pid = [int]$PID
        status = "STOPPED"
        restart_count = 0
        last_error = $null
        last_check_at = $null
        updated_at = [DateTime]::UtcNow.ToString("o")
    }
    foreach ($key in $values.Keys) { $payload[$key] = $values[$key] }
    Write-AtomicText $script:WatchdogStatePath ($payload | ConvertTo-Json -Depth 5)
}

function Read-NativeWatchdogState {
    if (-not (Test-Path -LiteralPath $script:WatchdogStatePath -PathType Leaf)) { return $null }
    try { return Get-Content -Raw -LiteralPath $script:WatchdogStatePath | ConvertFrom-Json } catch { return $null }
}

function Assert-ExternalRoot {
    $source = ([IO.Path]::GetFullPath($script:SourceRoot)).TrimEnd("\") + "\"
    $runtime = ([IO.Path]::GetFullPath($script:Root)).TrimEnd("\") + "\"
    if ($runtime.StartsWith($source, [StringComparison]::OrdinalIgnoreCase)) {
        throw "ASHARE_NATIVE_ROOT must be outside the source checkout: $runtime"
    }
}

function Initialize-Directories {
    @(
        $script:Root,
        (Join-Path $script:Root "bin"),
        (Join-Path $script:Root "config"),
        (Join-Path $script:Root "data"),
        (Join-Path $script:Root "data\lake"),
        (Join-Path $script:Root "data\objects"),
        (Join-Path $script:Root "data\private"),
        (Join-Path $script:Root "data\postgres"),
        (Join-Path $script:Root "data\redis"),
        (Join-Path $script:Root "deps"),
        (Join-Path $script:Root "downloads"),
        (Join-Path $script:Root "logs"),
        (Join-Path $script:Root "migrations"),
        (Join-Path $script:Root "configs"),
        (Join-Path $script:Root "state"),
        (Join-Path $script:Root "web"),
        (Join-Path $script:Root "build")
    ) | ForEach-Object {
        New-Item -ItemType Directory -Force -Path $_ | Out-Null
    }
}

function Get-ListeningProcessIds([int]$port) {
    # Prefer netstat only. Get-NetTCPConnection can hang for a long time on some
    # Windows hosts and would freeze both start and the service-account watchdog loop.
    $ids = @()
    $needle = ":$port"
    $lines = @(netstat.exe -ano -p TCP 2>$null)
    foreach ($line in $lines) {
        $text = $line.ToString().Trim()
        if ($text -notmatch "LISTENING") { continue }
        if ($text -notmatch [regex]::Escape($needle)) { continue }
        $parts = @($text -split "\s+")
        if ($parts.Count -lt 4) { continue }
        $local = $parts[1]
        if ($local -notmatch (":$port$")) { continue }
        if ($parts[-1] -match "^\d+$") {
            $ids += [int]$parts[-1]
        }
    }
    return @($ids | Sort-Object -Unique)
}

function Get-ProcessDescriptions([int[]]$ids) {
    if (-not $ids) { return @() }
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    return @($all | Where-Object { $ids -contains [int]$_.ProcessId } | ForEach-Object {
        "PID $($_.ProcessId) $($_.Name)"
    })
}

function Get-NativePortCandidates([string]$role) {
    switch ($role) {
        "postgres" { return @(55432, 55433, 55434, 55600, 55601, 55602) }
        "redis" { return @(56379, 56380, 56381, 55610, 55611) }
        "api" { return @(58000, 58001, 58002, 55620, 55621) }
        "searxng" { return @(58080, 58081, 58082, 55630, 55631) }
        default { throw "unknown native port role: $role" }
    }
}

function Write-NativePortConfig {
    $ports = [ordered]@{
        postgres = $script:PostgresPort
        redis = $script:RedisPort
        api = $script:ApiPort
        searxng = $script:SearxngPort
    }
    Write-AtomicText $script:PortsPath ($ports | ConvertTo-Json -Depth 5)
    $pathsFile = Join-Path $script:Root "config\native-paths.json"
    if (Test-Path -LiteralPath $pathsFile -PathType Leaf) {
        $paths = Get-Content -Raw -LiteralPath $pathsFile | ConvertFrom-Json
        $paths.postgres_port = $script:PostgresPort
        $paths.redis_port = $script:RedisPort
        $paths.api_port = $script:ApiPort
        $paths.searxng_port = $script:SearxngPort
        Write-AtomicText $pathsFile ($paths | ConvertTo-Json -Depth 8)
    }
}

function Update-NativeEnvPorts {
    if (-not (Test-Path -LiteralPath $script:EnvPath -PathType Leaf)) { return }
    $lines = @(Get-Content -LiteralPath $script:EnvPath)
    $updated = foreach ($line in $lines) {
        if ($line -like "DATABASE_URL=*") {
            [regex]::Replace($line, "@127\.0\.0\.1:\d+/", "@127.0.0.1:$script:PostgresPort/")
        } elseif ($line -like "REDIS_URL=*") {
            [regex]::Replace($line, "@127\.0\.0\.1:\d+/", "@127.0.0.1:$script:RedisPort/")
        } elseif ($line -like "SEARXNG_BASE_URL=*") {
            [regex]::Replace($line, "127\.0\.0\.1:\d+", "127.0.0.1:$script:SearxngPort")
        } else {
            $line
        }
    }
    Write-AtomicText $script:EnvPath ($updated -join [Environment]::NewLine)
}

function Read-Manifest {
    if (-not (Test-Path -LiteralPath $script:ManifestPath -PathType Leaf)) {
        throw "native dependency lock is missing: $script:ManifestPath"
    }
    return Get-Content -Raw -LiteralPath $script:ManifestPath | ConvertFrom-Json
}

function Test-NativePortAvailable([int]$port) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
    $started = $false
    try {
        $listener.Start()
        $started = $true
        return $true
    } catch {
        return $false
    } finally {
        if ($started) { $listener.Stop() }
    }
}

function Select-NativePort([int[]]$candidates, [int[]]$used = @()) {
    foreach ($candidate in $candidates) {
        if (($used -notcontains $candidate) -and (Test-NativePortAvailable $candidate)) {
            return $candidate
        }
    }
    throw ("no free native port found in candidates: {0}" -f ($candidates -join ","))
}

function Assert-NativePortFree([int]$port) {
    $owners = @(Get-ListeningProcessIds $port)
    if ($owners.Count -gt 0 -or -not (Test-NativePortAvailable $port)) {
        $details = (Get-ProcessDescriptions $owners) -join ", "
        if ([string]::IsNullOrWhiteSpace($details)) { $details = "unknown process" }
        throw "native port $port is occupied: $details"
    }
}

function Assert-NativePortOwned([int]$port, $services, [int]$attempts = 10) {
    $allowed = [System.Collections.Generic.HashSet[int]]::new()
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    foreach ($service in @($services)) {
        if ($service.pid) {
            foreach ($id in (Get-ProcessTreeIds ([int]$service.pid) $all)) {
                [void]$allowed.Add([int]$id)
            }
        }
    }
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        $owners = @(Get-ListeningProcessIds $port)
        if ($owners.Count -eq 0) {
            Start-Sleep -Seconds 1
            continue
        }
        if (@($owners | Where-Object { $allowed.Contains([int]$_) }).Count -gt 0) { return }
        # If a managed listener is not yet visible under the service account, keep
        # waiting rather than treating a foreign process as permanent failure.
        Start-Sleep -Seconds 1
    }
    $details = (Get-ProcessDescriptions (Get-ListeningProcessIds $port)) -join ", "
    if ([string]::IsNullOrWhiteSpace($details)) { $details = "no managed listener" }
    throw "native port $port is not owned by the expected process group: $details"
}

function Reconcile-NativePorts {
    $current = [ordered]@{
        postgres = $script:PostgresPort
        redis = $script:RedisPort
        api = $script:ApiPort
        searxng = $script:SearxngPort
    }
    $used = @()
    $selected = [ordered]@{}
    foreach ($role in @("postgres", "redis", "api", "searxng")) {
        $port = [int]$current[$role]
        $free = ($used -notcontains $port) -and (@(Get-ListeningProcessIds $port).Count -eq 0) -and (Test-NativePortAvailable $port)
        if ($free) {
            $selected[$role] = $port
        } else {
            $selected[$role] = Select-NativePort (Get-NativePortCandidates $role) $used
            Write-NativeEvent ("rotating occupied {0} port {1} to {2}" -f $role, $port, $selected[$role]) "WARN"
        }
        $used += [int]$selected[$role]
    }
    $changed = ($selected.postgres -ne $script:PostgresPort -or
        $selected.redis -ne $script:RedisPort -or
        $selected.api -ne $script:ApiPort -or
        $selected.searxng -ne $script:SearxngPort)
    $script:PostgresPort = [int]$selected.postgres
    $script:RedisPort = [int]$selected.redis
    $script:ApiPort = [int]$selected.api
    $script:SearxngPort = [int]$selected.searxng
    if ($changed) {
        Update-NativeEnvPorts
        Write-NativePortConfig
        Write-NativeEvent "persisted a new conflict-free native port set" "WARN"
    }
    foreach ($port in @($script:PostgresPort, $script:RedisPort, $script:ApiPort, $script:SearxngPort)) {
        Assert-NativePortFree $port
    }
}

function Initialize-NativePorts {
    $existing = $null
    if (Test-Path -LiteralPath $script:PortsPath -PathType Leaf) {
        try { $existing = Get-Content -Raw -LiteralPath $script:PortsPath | ConvertFrom-Json } catch { $existing = $null }
    }
    $existingPorts = @()
    if ($existing -and $existing.postgres -and $existing.redis -and $existing.api -and $existing.searxng) {
        $existingPorts = @([int]$existing.postgres, [int]$existing.redis, [int]$existing.api, [int]$existing.searxng)
    }
    $existingUsable = $existingPorts.Count -eq 4 -and
        (@($existingPorts | Where-Object { $_ -le 0 -or @(Get-ListeningProcessIds $_).Count -gt 0 -or -not (Test-NativePortAvailable $_) }).Count -eq 0) -and
        (@($existingPorts | Select-Object -Unique).Count -eq 4)
    if ($existingUsable) {
        $script:PostgresPort = [int]$existing.postgres
        $script:RedisPort = [int]$existing.redis
        $script:ApiPort = [int]$existing.api
        $script:SearxngPort = [int]$existing.searxng
        return
    }
    if ($existingPorts.Count -eq 4) {
        Write-NativeEvent "saved native ports are occupied; selecting a new port set" "WARN"
    }
    $script:PostgresPort = Select-NativePort @(55432, 55433, 55434, 55600, 55601, 55602)
    $script:RedisPort = Select-NativePort @(56379, 56380, 56381, 55610, 55611) @($script:PostgresPort)
    $script:ApiPort = Select-NativePort @(58000, 58001, 58002, 55620, 55621) @($script:PostgresPort, $script:RedisPort)
    $script:SearxngPort = Select-NativePort @(58080, 58081, 58082, 55630, 55631) @($script:PostgresPort, $script:RedisPort, $script:ApiPort)
}

function Get-ArtifactChecksum($artifact) {
    if ($artifact.sha256 -and $artifact.sha256 -match "^[0-9a-fA-F]{64}$") {
        return $artifact.sha256.ToLowerInvariant()
    }
    if ($artifact.checksum_url) {
        $checksumText = (Invoke-WebRequest -UseBasicParsing -Uri $artifact.checksum_url).Content
        $match = [regex]::Match($checksumText, "(?i)([0-9a-f]{64})")
        if ($match.Success) {
            return $match.Groups[1].Value.ToLowerInvariant()
        }
    }
    if ($artifact.github_release_api) {
        $release = Invoke-RestMethod -Headers @{
            Accept = "application/vnd.github+json"
            "User-Agent" = "ashare-ai-native-installer/$script:NativeVersion"
        } -Uri $artifact.github_release_api
        $asset = @($release.assets) | Where-Object { $_.name -eq $artifact.asset_name } | Select-Object -First 1
        if ($asset -and $asset.digest -and $asset.digest -match "(?i)^sha256:[0-9a-f]{64}$") {
            return $asset.digest.Substring(7).ToLowerInvariant()
        }
    }
    throw "no trusted SHA-256 source is available for $($artifact.id) $($artifact.version)"
}

function Get-VerifiedArchive($artifact) {
    $target = Join-Path $script:Root ("downloads\{0}-{1}.zip" -f $artifact.id, $artifact.version)
    $expected = Get-ArtifactChecksum $artifact
    $needsDownload = $true
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
        $needsDownload = $actual -ne $expected
    }
    if ($needsDownload) {
        if (Test-Path -LiteralPath $target) {
            Remove-Item -Force -LiteralPath $target
        }
        Invoke-WebRequest -UseBasicParsing -Uri $artifact.url -OutFile $target
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        Remove-Item -Force -LiteralPath $target
        throw "SHA-256 verification failed for $($artifact.id): expected $expected, got $actual"
    }
    return $target
}

function Expand-VerifiedArtifact($artifact, [string]$archivePath) {
    $destination = Join-Path $script:Root ("deps\{0}\{1}" -f $artifact.id, $artifact.version)
    if (-not (Test-Path -LiteralPath $destination -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
        Expand-Archive -LiteralPath $archivePath -DestinationPath $destination -Force
    }
    return $destination
}

function Install-Searxng($artifact) {
    if (-not $artifact.commit -or -not $artifact.archive_url -or -not $artifact.sha256) { throw "SearXNG artifact lock is incomplete" }
    $destination = Join-Path $script:Root ("deps\searxng\{0}" -f $artifact.version)
    $archivePath = Join-Path $script:Root ("downloads\searxng-{0}.zip" -f $artifact.version)
    $bundledArchive = Join-Path $script:SourceRoot "vendor\searxng.zip"
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $archivePath) | Out-Null
        if (Test-Path -LiteralPath $bundledArchive -PathType Leaf) { Copy-Item -Force -LiteralPath $bundledArchive -Destination $archivePath }
        else { Invoke-WebRequest -UseBasicParsing -Uri $artifact.archive_url -OutFile $archivePath }
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash
    if (-not $actualHash.Equals([string]$artifact.sha256, [StringComparison]::OrdinalIgnoreCase)) { throw "SearXNG archive checksum mismatch: expected $($artifact.sha256), got $actualHash" }
    if (Test-Path -LiteralPath $destination) { Remove-Item -Recurse -Force -LiteralPath $destination }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    $destinationRoot = ([IO.Path]::GetFullPath($destination)).TrimEnd("\") + "\"
    try {
        foreach ($entry in $zip.Entries) {
            $parts = $entry.FullName -split "/", 2
            if ($parts.Count -lt 2) { continue }
            $relative = $parts[1]
            if ([string]::IsNullOrWhiteSpace($relative) -or $relative -eq "utils" -or $relative.StartsWith("utils/")) { continue }
            $target = [IO.Path]::GetFullPath((Join-Path $destination $relative.Replace("/", "\")))
            if (-not $target.StartsWith($destinationRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "SearXNG archive path escapes destination: $relative" }
            if ($entry.FullName.EndsWith("/")) { New-Item -ItemType Directory -Force -Path $target | Out-Null }
            else { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null; [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true) }
        }
    } finally { $zip.Dispose() }
    if (-not (Test-Path -LiteralPath (Join-Path $destination "setup.py") -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $destination "searx\__init__.py") -PathType Leaf)) { throw "SearXNG runtime source extraction is incomplete" }
    @("import getpass", "import os", "", "class _PasswdEntry:", "    def __init__(self, name, uid):", "        self.pw_name = name", "        self.pw_uid = uid", "", "def getpwuid(uid):", "    return _PasswdEntry(getpass.getuser() or os.environ.get('USERNAME', 'unknown'), uid)") | Set-Content -LiteralPath (Join-Path $destination "pwd.py") -Encoding ASCII
    return $destination
}

function Find-Executable([string]$root, [string]$name) {
    $file = Get-ChildItem -LiteralPath $root -Recurse -File -Filter $name | Select-Object -First 1
    if (-not $file) {
        throw "$name was not found under $root"
    }
    return $file.FullName
}

function New-RandomHex([int]$bytes = 24) {
    $buffer = New-Object byte[] $bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    } finally {
        $rng.Dispose()
    }
    return ([BitConverter]::ToString($buffer) -replace "-", "").ToLowerInvariant()
}

function ConvertTo-FernetKey {
    $buffer = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    } finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($buffer).Replace("+", "-").Replace("/", "_")
}

function Get-NativeIdentityMode {
    # Where the watchdog and service processes run. Values: "task" (built-in
    # NETWORK SERVICE on Windows / current user on Linux — default), or
    # "account" (dedicated local account).
    # Stored in config/runtime-identity.json so the web settings API and the
    # native controllers share one source of truth.
    $path = Join-Path $script:Root "config\runtime-identity.json"
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        try {
            $value = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
            if ($value.mode -in @("task", "account")) { return [string]$value.mode }
        } catch { }
    }
    return "task"
}

function Get-NativeServiceAccountName {
    return "AshareAIService"
}

function Ensure-NativeServiceAccount([string]$pythonRoot) {
    $accountName = Get-NativeServiceAccountName
    $passwordPath = Join-Path $script:Root "config\service-password.txt"
    $password = if (Test-Path -LiteralPath $passwordPath -PathType Leaf) {
        (Get-Content -LiteralPath $passwordPath -Raw).Trim()
    } else {
        $null
    }
    if ([string]::IsNullOrWhiteSpace($password) -or $password.Length -gt 14) {
        # net.exe asks a Y/N prompt when the password exceeds 14 characters (LAN
        # Manager compatibility) and reads that answer from the console, NOT from
        # redirected stdin, so it hangs under a scripted install ("没有提供有效
        # 的响应"). Keep the account password at most 14 characters
        # ("A" + 10 hex + "!a1") to avoid the prompt entirely.
        $password = "A$(New-RandomHex 5)!a1"
    }
    # EAP=Stop turns REDIRECTED native stderr into a terminating
    # NativeCommandError in PS 5.1 (same idiom as Wait-PostgresReady). "Account
    # not found" is the normal create case, so probe and update with EAP dropped
    # to Continue and gate everything on $LASTEXITCODE.
    $oldEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & net.exe user $accountName 2>$null | Out-Null
        $accountExists = $LASTEXITCODE -eq 0
        if ($accountExists) {
            & net.exe user $accountName $password "/active:yes" "/passwordchg:no" "/expires:never" | Out-Null
        } else {
            & net.exe user $accountName $password "/add" "/active:yes" "/passwordchg:no" "/expires:never" | Out-Null
        }
    } finally {
        $ErrorActionPreference = $oldEap
    }
    if ($LASTEXITCODE -ne 0) {
        throw "could not create or update the native service account; run install from an elevated PowerShell"
    }
    [System.IO.File]::WriteAllText($passwordPath, $password, [System.Text.UTF8Encoding]::new($false))
    return $accountName
}

function Read-NativeServiceCredential {
    $accountName = Get-NativeServiceAccountName
    $passwordPath = Join-Path $script:Root "config\service-password.txt"
    if (-not (Test-Path -LiteralPath $passwordPath -PathType Leaf)) {
        throw "native service account is missing; run install first"
    }
    $password = (Get-Content -LiteralPath $passwordPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($password)) {
        throw "native service account password is empty"
    }
    $principal = "{0}\{1}" -f $env:COMPUTERNAME, $accountName
    $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
    return [PSCredential]::new($principal, $securePassword)
}

function Read-NativeServicePassword {
    $passwordPath = Join-Path $script:Root "config\service-password.txt"
    if (-not (Test-Path -LiteralPath $passwordPath -PathType Leaf)) {
        throw "native service account is missing; run install first"
    }
    $password = (Get-Content -LiteralPath $passwordPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($password)) {
        throw "native service account password is empty"
    }
    return $password
}

function Ensure-NativeBatchLogonRight {
    # Register-ScheduledTask -User/-Password stores the credential but does NOT
    # grant "Log on as a batch job" (SeBatchLogonRight); without it the task
    # fails 0x80070569 (ERROR_LOGON_TYPE_NOT_GRANTED). Grant idempotently via
    # LsaAddAccountRights. Only used in account mode.
    $accountName = Get-NativeServiceAccountName
    $csharp = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class NativeBatchLogon
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_UNICODE_STRING
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_OBJECT_ATTRIBUTES
    {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint LsaOpenPolicy(IntPtr SystemName, ref LSA_OBJECT_ATTRIBUTES ObjectAttributes, uint DesiredAccess, out IntPtr PolicyHandle);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint LsaAddAccountRights(IntPtr PolicyHandle, byte[] AccountSid, LSA_UNICODE_STRING[] UserRights, uint CountOfRights);
    [DllImport("advapi32.dll")]
    private static extern int LsaClose(IntPtr ObjectHandle);
    [DllImport("advapi32.dll")]
    private static extern uint LsaNtStatusToWinError(uint Status);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool LookupAccountName(string lpSystemName, string lpAccountName, byte[] Sid, ref uint cbSid, StringBuilder ReferencedDomainName, ref uint cchReferencedDomainName, out int peUse);

    public static void Grant(string accountName)
    {
        byte[] sid = new byte[1024];
        uint cbSid = (uint)sid.Length;
        StringBuilder domain = new StringBuilder(256);
        uint cbDomain = (uint)domain.Capacity;
        int use;
        if (!LookupAccountName(null, accountName, sid, ref cbSid, domain, ref cbDomain, out use))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        Array.Resize(ref sid, (int)cbSid);
        LSA_OBJECT_ATTRIBUTES attrs = new LSA_OBJECT_ATTRIBUTES();
        attrs.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        // LsaAddAccountRights needs POLICY_CREATE_ACCOUNT; POLICY_ALL_ACCESS covers
        // it and is available to an elevated admin token.
        const uint POLICY_ALL_ACCESS = 0xF0FFF;
        uint status = LsaOpenPolicy(IntPtr.Zero, ref attrs, POLICY_ALL_ACCESS, out policy);
        if (status != 0)
            throw new Win32Exception((int)LsaNtStatusToWinError(status));
        try
        {
            LSA_UNICODE_STRING[] rights = new LSA_UNICODE_STRING[1];
            rights[0].Buffer = Marshal.StringToHGlobalUni("SeBatchLogonRight");
            rights[0].Length = (ushort)("SeBatchLogonRight".Length * 2);
            rights[0].MaximumLength = (ushort)(("SeBatchLogonRight".Length + 1) * 2);
            uint st = LsaAddAccountRights(policy, sid, rights, 1);
            Marshal.FreeHGlobal(rights[0].Buffer);
            if (st != 0)
                throw new Win32Exception((int)LsaNtStatusToWinError(st));
        }
        finally
        {
            LsaClose(policy);
        }
    }
}
'@
    if (-not ("NativeBatchLogon" -as [type])) {
        Add-Type -TypeDefinition $csharp -ErrorAction Stop
    }
    [NativeBatchLogon]::Grant($accountName)
    Write-NativeEvent "granted SeBatchLogonRight to $accountName"
}

function Protect-NativeRuntime([string]$pythonRoot = $null, [string]$mode = "task") {
    $currentPrincipal = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    # icacls drops an inheritable "(OI)(CI)" grant when it lands on a leaf FILE,
    # which leaves files with an EMPTY DACL (everyone denied) once the inherited
    # ACEs are stripped by /inheritance:r. Grant each principal BOTH an
    # inheritable form (applied to directories, inherited by future children)
    # and a plain form (applied to existing leaf files); icacls keeps the right
    # one per object type. SYSTEM (*S-1-5-18) and NETWORK SERVICE (*S-1-5-20) are
    # always granted; account mode additionally grants the dedicated service
    # account (PostgreSQL refuses any administrative token, so the process
    # identity must be unprivileged).
    $rules = @(
        ("{0}:(OI)(CI)F" -f $currentPrincipal), ("{0}:F" -f $currentPrincipal),
        "*S-1-5-18:(OI)(CI)F", "*S-1-5-18:F",
        "*S-1-5-20:(OI)(CI)F", "*S-1-5-20:F",
        "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-32-544:F"
    )
    if ($mode -eq "account") {
        $servicePrincipal = "{0}\{1}" -f $env:COMPUTERNAME, (Get-NativeServiceAccountName)
        $rules += @(("{0}:(OI)(CI)F" -f $servicePrincipal), ("{0}:F" -f $servicePrincipal))
    }
    & icacls.exe $script:Root /inheritance:r /grant:r $rules /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "could not restrict the native runtime directory ACL"
    }
    if ($pythonRoot -and (Test-Path -LiteralPath $pythonRoot -PathType Container)) {
        # The base interpreter can live outside the runtime (a per-user Python
        # install under the profile); the service identity needs read+execute on
        # it to run the services. /grant:r here only touches the listed ACEs, it
        # does not reset inheritance on the user's Python directory.
        $pythonGrants = @("*S-1-5-20:(OI)(CI)RX")
        if ($mode -eq "account") {
            $servicePrincipal = "{0}\{1}" -f $env:COMPUTERNAME, (Get-NativeServiceAccountName)
            $pythonGrants += ("{0}:(OI)(CI)RX" -f $servicePrincipal)
        }
        & icacls.exe $pythonRoot "/grant:r" $pythonGrants /C | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "could not grant the service identity access to the host Python runtime"
        }
    }
}

function Remove-NativeLegacyServiceAccount {
    # Only meaningful when the dedicated account exists (leaving account mode):
    # the watchdog task's stored password references that account, so the task
    # must be unregistered before the account is deleted. When the account does
    # not exist (task/system mode), this is a no-op — the task is left untouched
    # and Register-NativeWatchdogTask re-registers it with the mode's principal
    # (-Force replaces the principal without an unregister, avoiding a window
    # where the watchdog task briefly does not exist).
    $accountName = "AshareAIService"
    $oldEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & net.exe user $accountName 2>$null | Out-Null
        $accountExists = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $oldEap
    }
    if ($accountExists) {
        try { Stop-ScheduledTask -TaskName $script:WatchdogTaskName -ErrorAction SilentlyContinue } catch { }
        Unregister-ScheduledTask -TaskName $script:WatchdogTaskName -Confirm:$false -ErrorAction SilentlyContinue
        & net.exe user $accountName "/delete" 2>$null | Out-Null
        Write-NativeEvent "removed native service account $accountName"
    }
    $passwordPath = Join-Path $script:Root "config\service-password.txt"
    if (Test-Path -LiteralPath $passwordPath -PathType Leaf) {
        Remove-Item -LiteralPath $passwordPath -Force
        Write-NativeEvent "removed native service password file"
    }
}

function Apply-NativeIdentity([string]$pythonRoot = $null) {
    # Reconcile the account / ACLs / watchdog task to the mode chosen in the web
    # system settings (config/runtime-identity.json). Idempotent; runs on every
    # elevated install / repair / start so a mode change takes effect on the next
    # administrator command.
    $mode = Get-NativeIdentityMode
    if (-not $pythonRoot) {
        $pathsFile = Join-Path $script:Root "config\native-paths.json"
        if (Test-Path -LiteralPath $pathsFile -PathType Leaf) {
            $paths = Get-Content -Raw -LiteralPath $pathsFile | ConvertFrom-Json
            if ($paths.python_exe) { $pythonRoot = Split-Path -Parent ([string]$paths.python_exe) }
        }
    }
    if ($mode -eq "account") {
        Ensure-NativeServiceAccount $pythonRoot
        Ensure-NativeBatchLogonRight
    } else {
        Remove-NativeLegacyServiceAccount
    }
    Protect-NativeRuntime $pythonRoot $mode
    Register-NativeWatchdogTask
    Write-NativeEvent "applied native identity mode $mode"
}

function Write-NativeEnv([string]$postgresPassword, [string]$redisPassword, [string]$adminPassword, [string]$fernetKey) {
    $postgresPort = $script:PostgresPort
    $redisPort = $script:RedisPort
    $apiPort = $script:ApiPort
    $searxngPort = $script:SearxngPort
    $envText = @"
APP_ENV=development
DATABASE_URL=postgresql+psycopg://ashare:$postgresPassword@127.0.0.1:$postgresPort/ashare
REDIS_URL=redis://:$redisPassword@127.0.0.1:$redisPort/0
LAKE_ROOT=$($script:Root.Replace('\', '/'))/data/lake
PRIVATE_OBJECT_ROOT=$($script:Root.Replace('\', '/'))/data/private
POLICY_CONFIG_PATH=$($script:Root.Replace('\', '/'))/configs/first_release.v3.json
DEPENDENCY_LOCK_PATH=$($script:Root.Replace('\', '/'))/requirements.lock
NATIVE_WEB_ROOT=$($script:Root.Replace('\', '/'))/web
SEARXNG_BASE_URL=http://127.0.0.1:$searxngPort
TRUSTED_HOSTS=127.0.0.1,localhost
WEB_BIND_ADDRESS=127.0.0.1
ADMIN_USERNAME=$AdminUsername
ADMIN_PASSWORD=$adminPassword
MODEL_SETTINGS_ENCRYPTION_KEYS=$fernetKey
MARKET_CACHE_SECONDS=5
MARKET_KLINE_CACHE_SECONDS=300
MARKET_PREFETCH_MAX_WORKERS=3
MARKET_PROVIDER_MAX_WORKERS=3
MARKET_PROVIDER_MAX_QUEUE=6
OPENBLAS_NUM_THREADS=1
OMP_NUM_THREADS=1
MKL_NUM_THREADS=1
NUMEXPR_NUM_THREADS=1
ARROW_IO_THREADS=1
PYTHONUNBUFFERED=1
"@
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($script:EnvPath, $envText.Trim(), $utf8NoBom)
    [System.IO.File]::WriteAllText((Join-Path $script:Root "config\postgres-password.txt"), $postgresPassword, $utf8NoBom)
    [System.IO.File]::WriteAllText((Join-Path $script:Root "config\redis-password.txt"), $redisPassword, $utf8NoBom)
    [System.IO.File]::WriteAllText((Join-Path $script:Root "config\admin-credentials.txt"), ("username=$AdminUsername`npassword=$adminPassword"), $utf8NoBom)
}

function Write-NativeSearxSettings([string]$redisPassword) {
    $settingsPath = Join-Path $script:Root "config\searxng-settings.yml"
    $settings = @"
use_default_settings: true

server:
  secret_key: "ashare-internal-search-no-public-session-v1"
  limiter: false
  image_proxy: false
  bind_address: 127.0.0.1
  port: $script:SearxngPort

valkey:
  url: "redis://:$redisPassword@127.0.0.1:$script:RedisPort/0"

search:
  safe_search: 1
  formats:
    - html
    - json
"@
    # UTF-8 without BOM: Set-Content -Encoding UTF8 would prepend a BOM and PyYAML
    # would parse the first key as "﻿use_default_settings".
    [System.IO.File]::WriteAllText($settingsPath, $settings, [System.Text.UTF8Encoding]::new($false))
}

function Invoke-NativePython([string[]]$arguments) {
    $python = Join-Path $script:Root "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "native Python environment is missing; run install first"
    }
    Push-Location $script:Root
    try {
        $oldEap = $ErrorActionPreference
        try {
            # venv Python prints warnings to stderr; under EAP=Stop those become a
            # terminating NativeCommandError even when the command succeeds. The
            # explicit exit-code check below is the real gate.
            $ErrorActionPreference = "Continue"
            & $python @arguments
        } finally {
            $ErrorActionPreference = $oldEap
        }
        if ($LASTEXITCODE -ne 0) {
            throw "native Python command failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
}

function Set-NativeProcessEnvironment([hashtable]$values) {
    foreach ($key in $values.Keys) {
        Set-Item -Path ("Env:{0}" -f $key) -Value ([string]$values[$key])
    }
}

function Read-State {
    if (-not (Test-Path -LiteralPath $script:StatePath -PathType Leaf)) {
        return @()
    }
    try {
        $value = Get-Content -Raw -LiteralPath $script:StatePath | ConvertFrom-Json
        return @($value.services)
    } catch {
        return @()
    }
}

function Write-State($services) {
    $payload = [ordered]@{
        version = 1
        runtime_version = $script:NativeVersion
        root = $script:Root
        updated_at = [DateTime]::UtcNow.ToString("o")
        services = @($services)
    }
    Write-AtomicText $script:StatePath ($payload | ConvertTo-Json -Depth 8)
}

function Get-ProcessTreeIds([int]$rootProcessId, $allProcesses) {
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    [void]$ids.Add($rootProcessId)
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $allProcesses) {
            if ($ids.Contains([int]$process.ParentProcessId) -and $ids.Add([int]$process.ProcessId)) {
                $changed = $true
            }
        }
    }
    return @($ids)
}

function Stop-NativeProcessTree([int]$rootProcessId) {
    if ($rootProcessId -le 0) { return }
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $ids = @(Get-ProcessTreeIds $rootProcessId $all | Sort-Object -Descending)
    foreach ($id in $ids) {
        $process = Get-Process -Id ([int]$id) -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-NativePortsFromFile {
    if (-not (Test-Path -LiteralPath $script:PortsPath -PathType Leaf)) { return $false }
    try {
        $ports = Get-Content -Raw -LiteralPath $script:PortsPath | ConvertFrom-Json
        if (-not $ports.postgres -or -not $ports.redis -or -not $ports.api -or -not $ports.searxng) {
            return $false
        }
        $script:PostgresPort = [int]$ports.postgres
        $script:RedisPort = [int]$ports.redis
        $script:ApiPort = [int]$ports.api
        $script:SearxngPort = [int]$ports.searxng
        return $true
    } catch {
        return $false
    }
}

function Get-NativeInstallationState {
    $missing = @()
    $pathsFile = Join-Path $script:Root "config\native-paths.json"
    if (-not (Test-Path -LiteralPath $script:EnvPath -PathType Leaf)) { $missing += ".env" }
    if (-not (Test-Path -LiteralPath $pathsFile -PathType Leaf)) { $missing += "config/native-paths.json" }
    if (-not (Test-Path -LiteralPath (Join-Path $script:Root "venv\Scripts\python.exe") -PathType Leaf)) { $missing += "venv" }
    if (-not (Test-Path -LiteralPath (Join-Path $script:Root "web\index.html") -PathType Leaf)) { $missing += "web/index.html" }
    if (-not (Get-NativePortsFromFile)) { $missing += "config/native-ports.json" }
    return [pscustomobject]@{
        ready = $missing.Count -eq 0
        status = if ($missing.Count -eq 0) { "READY" } else { "NOT_INSTALLED" }
        missing = @($missing)
    }
}

function Get-NativeServicePort([string]$role) {
    switch ($role) {
        "postgres" { return $script:PostgresPort }
        "redis" { return $script:RedisPort }
        "api" { return $script:ApiPort }
        "searxng" { return $script:SearxngPort }
        default { return 0 }
    }
}

function Test-NativeServiceHealthy($service, $services) {
    if ($service.embedded_in) { return $true }
    if (-not (Test-ManagedProcess $service)) { return $false }
    $port = Get-NativeServicePort ([string]$service.role)
    if ($port -gt 0) {
        $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
        $allowed = @(Get-ProcessTreeIds ([int]$service.pid) $all)
        $owners = @(Get-ListeningProcessIds $port)
        if ($owners.Count -gt 0 -and @($owners | Where-Object { $allowed -contains [int]$_ }).Count -eq 0) {
            return $false
        }
    }
    try {
        switch ([string]$service.role) {
            "api" {
                $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$script:ApiPort/api/v1/health"
                return $response.StatusCode -eq 200
            }
            "searxng" {
                $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$script:SearxngPort/healthz"
                return $response.StatusCode -eq 200
            }
            "postgres" {
                $pathsFile = Join-Path $script:Root "config\native-paths.json"
                if (-not (Test-Path -LiteralPath $pathsFile -PathType Leaf)) { return $false }
                $paths = Get-Content -Raw -LiteralPath $pathsFile | ConvertFrom-Json
                $pgIsReady = Join-Path $paths.postgres_bin "pg_isready.exe"
                & $pgIsReady -h 127.0.0.1 -p $script:PostgresPort -U ashare 2>$null | Out-Null
                return $LASTEXITCODE -eq 0
            }
            "redis" {
                $pathsFile = Join-Path $script:Root "config\native-paths.json"
                if (-not (Test-Path -LiteralPath $pathsFile -PathType Leaf)) { return $false }
                $paths = Get-Content -Raw -LiteralPath $pathsFile | ConvertFrom-Json
                $redisCli = Join-Path $paths.redis_bin "redis-cli.exe"
                $password = (Get-Content (Join-Path $script:Root "config\redis-password.txt") -Raw).Trim()
                $oldRedisAuth = $env:REDISCLI_AUTH
                try {
                    $env:REDISCLI_AUTH = $password
                    $reply = & $redisCli -h 127.0.0.1 -p $script:RedisPort ping 2>$null
                    return ($LASTEXITCODE -eq 0 -and "$reply".Trim() -eq "PONG")
                } finally {
                    if ($null -eq $oldRedisAuth) { Remove-Item Env:REDISCLI_AUTH -ErrorAction SilentlyContinue }
                    else { $env:REDISCLI_AUTH = $oldRedisAuth }
                }
            }
            default {
                return $true
            }
        }
    } catch {
        return $false
    }
}

function Test-NativeRuntimeHealthy($services) {
    $managed = @($services | Where-Object { -not $_.embedded_in })
    if ($managed.Count -eq 0) { return $false }
    foreach ($service in $managed) {
        if (-not (Test-NativeServiceHealthy $service $services)) { return $false }
    }
    return $true
}

function Get-NativePowerShellPath {
    $candidate = Join-Path $PSHOME "powershell.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    $command = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "Windows PowerShell executable was not found"
}

function Get-NativeTaskArguments {
    $controllerDirectory = Join-Path $script:Root "controller"
    New-Item -ItemType Directory -Force -Path $controllerDirectory | Out-Null
    $scriptPath = Join-Path $controllerDirectory "ashare-native.ps1"
    # When this script is already running from the controller copy, ScriptRoot
    # IS the controller directory and the source equals the destination; copying
    # a file onto itself fails with a sharing violation (the running file is
    # locked). Skip the self-copy in that case.
    $controllerSource = Join-Path $script:ScriptRoot "ashare-native.ps1"
    if ([IO.Path]::GetFullPath($controllerSource) -ne [IO.Path]::GetFullPath($scriptPath)) {
        Copy-Item -Force -LiteralPath $controllerSource -Destination $scriptPath
    }
    $lockPath = Join-Path $script:ScriptRoot "dependencies.lock.json"
    $lockDestination = Join-Path $controllerDirectory "dependencies.lock.json"
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        # Same self-copy guard as the .ps1 above: when this script already runs
        # from the controller directory, the lock file lives at the destination,
        # and Copy-Item onto itself throws a sharing violation.
        if ([IO.Path]::GetFullPath($lockPath) -ne [IO.Path]::GetFullPath($lockDestination)) {
            Copy-Item -Force -LiteralPath $lockPath -Destination $lockDestination
        }
    }
    $root = $script:Root.TrimEnd("\")
    $source = $script:SourceRoot.TrimEnd("\")
    return '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Command watchdog -Root "{1}" -SourceRoot "{2}" -WatchdogIntervalSeconds {3}' -f $scriptPath, $root, $source, $WatchdogIntervalSeconds
}

function Get-NativeTaskSummary([switch]$Fast) {
    if ($Fast) {
        $watchdog = Read-NativeWatchdogState
        return [pscustomobject]@{
            registered = Test-Path -LiteralPath $script:TaskNamePath -PathType Leaf
            state = if ($watchdog) { [string]$watchdog.status } else { "Unknown" }
            last_run_time = $null
            last_task_result = $null
            next_run_time = $null
            task_name = $script:WatchdogTaskName
        }
    }
    try {
        $task = Get-ScheduledTask -TaskName $script:WatchdogTaskName -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $script:WatchdogTaskName -ErrorAction Stop
        return [pscustomobject]@{
            registered = $true
            state = [string]$task.State
            last_run_time = $info.LastRunTime
            last_task_result = $info.LastTaskResult
            next_run_time = $info.NextRunTime
            task_name = $script:WatchdogTaskName
        }
    } catch {
        return [pscustomobject]@{
            registered = $false
            state = "Missing"
            last_run_time = $null
            last_task_result = $null
            next_run_time = $null
            task_name = $script:WatchdogTaskName
        }
    }
}

function Register-NativeWatchdogTask {
    $mode = Get-NativeIdentityMode
    $powerShell = Get-NativePowerShellPath
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument (Get-NativeTaskArguments) -WorkingDirectory $script:Root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 365)
    switch ($mode) {
        "account" {
            # Dedicated local account with password logon; needs the
            # SeBatchLogonRight grant from Ensure-NativeBatchLogonRight.
            $servicePrincipal = "{0}\{1}" -f $env:COMPUTERNAME, (Get-NativeServiceAccountName)
            $servicePassword = Read-NativeServicePassword
            Register-ScheduledTask -TaskName $script:WatchdogTaskName -Action $action -Trigger $trigger -Settings $settings -User $servicePrincipal -Password $servicePassword -RunLevel Limited -Force | Out-Null
            Write-NativeEvent "registered account watchdog task $($script:WatchdogTaskName)"
        }
        default {
            # Built-in NETWORK SERVICE account (default): unprivileged (so the
            # PostgreSQL children it launches are accepted), passwordless, works
            # at boot. TASK_LOGON_SERVICE_ACCOUNT needs no password and no
            # SeBatchLogonRight.
            $servicePrincipal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\NETWORK SERVICE" -LogonType ServiceAccount -RunLevel Highest
            Register-ScheduledTask -TaskName $script:WatchdogTaskName -Action $action -Trigger $trigger -Settings $settings -Principal $servicePrincipal -Force | Out-Null
            Write-NativeEvent "registered NETWORK SERVICE watchdog task $($script:WatchdogTaskName)"
        }
    }
    Write-AtomicText $script:TaskNamePath $script:WatchdogTaskName
}

function Ensure-NativeWatchdogTask {
    $summary = Get-NativeTaskSummary
    if (-not $summary.registered) {
        Register-NativeWatchdogTask
    }
}

function Start-NativeWatchdogTask {
    Ensure-NativeWatchdogTask
    Start-ScheduledTask -TaskName $script:WatchdogTaskName
    Write-NativeEvent "started watchdog task"
}

function Stop-NativeWatchdogTask {
    try { Stop-ScheduledTask -TaskName $script:WatchdogTaskName -ErrorAction SilentlyContinue } catch { }
    $watchdog = Read-NativeWatchdogState
    if ($watchdog -and $watchdog.pid -and (Get-Process -Id ([int]$watchdog.pid) -ErrorAction SilentlyContinue)) {
        Stop-NativeProcessTree ([int]$watchdog.pid)
    }
    Write-NativeWatchdogState @{ status = "STOPPED"; last_check_at = [DateTime]::UtcNow.ToString("o") }
}

function Get-WorkingSetReport($services, [switch]$Fast) {
    $all = if ($Fast) { @() } else { @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) }
    $live = @{}
    foreach ($process in @(Get-Process -ErrorAction SilentlyContinue)) {
        $live[[int]$process.Id] = $process
    }
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $rows = foreach ($service in $services) {
        $processId = [int]$service.pid
        $rootProcess = if ($processId -gt 0 -and $live.ContainsKey($processId)) { $live[$processId] } else { $null }
        $ids = if ($processId -gt 0 -and $rootProcess) {
            if ($Fast) { @($processId) } else { Get-ProcessTreeIds $processId $all }
        } else { @() }
        $bytes = [int64]0
        foreach ($id in $ids) {
            if ($seen.Add($id)) {
                try {
                    if ($live.ContainsKey([int]$id)) { $bytes += [int64]$live[[int]$id].WorkingSet64 }
                } catch { }
            }
        }
        [pscustomobject]@{
            service = $service.name
            role = $service.role
            pid = $processId
            healthy = ($null -ne $rootProcess)
            working_set_bytes = $bytes
            working_set_mib = [math]::Round($bytes / 1MB, 1)
            embedded_in = $service.embedded_in
        }
    }
    $total = [int64]0
    foreach ($row in $rows) { $total += [int64]$row.working_set_bytes }
    return [pscustomobject]@{
        collected_at = [DateTime]::UtcNow.ToString("o")
        scope = "NATIVE_PROCESS_GROUP"
        total_working_set_bytes = $total
        total_working_set_mib = [math]::Round($total / 1MB, 1)
        services = @($rows)
    }
}

function Test-NativeRuntimeHealthyFast($services) {
    $managed = @($services | Where-Object { -not $_.embedded_in })
    if ($managed.Count -eq 0) { return $false }
    foreach ($service in $managed) {
        $healthy = if ($service.PSObject.Properties.Name -contains "healthy") {
            [bool]$service.healthy
        } else {
            $service.pid -and (Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue)
        }
        if (-not $healthy) {
            return $false
        }
    }
    return $true
}

function Start-ManagedProcess([string]$name, [string]$filePath, [string[]]$arguments, [string]$workingDirectory, [hashtable]$environment = @{}, [string]$role = $name, [string]$embeddedIn = $null, [PSCredential]$credential = $null) {
    $old = @{}
    foreach ($key in $environment.Keys) {
        $old[$key] = (Get-Item -Path ("Env:{0}" -f $key) -ErrorAction SilentlyContinue).Value
        Set-Item -Path ("Env:{0}" -f $key) -Value ([string]$environment[$key])
    }
    try {
        $logBase = Join-Path $script:Root ("logs\{0}" -f $name)
        # Without -Credential (task mode) the child inherits the caller's
        # process AND environment, so the env vars set above reach the real
        # process directly. Account mode passes a credential and goes through the
        # launcher wrapper below.
        $startOptions = @{
            FilePath = $filePath
            ArgumentList = $arguments
            WorkingDirectory = $workingDirectory
            RedirectStandardOutput = "$logBase.out.log"
            RedirectStandardError = "$logBase.err.log"
            WindowStyle = "Hidden"
            PassThru = $true
        }
        if ($credential) {
            $startOptions.Credential = $credential
            $startOptions.LoadUserProfile = $true
            if ($environment.Count -gt 0) {
                # PowerShell 5.1 Start-Process -Credential launches the child with
                # the TARGET USER's default environment, NOT the caller's — so env
                # vars set above would never reach the real process (verified: the
                # service account's own PATH/USERNAME appear in the child, and a
                # caller-set variable is missing). Start-Process -Environment is
                # PS 6+ only. Workaround: run a launcher .ps1 as the service
                # account that sets each variable inside its own process, then
                # runs the real command as a direct synchronous child. The
                # launcher PID is the tracked pid, so Stop-NativeProcessTree still
                # tears down the whole tree, and the redirected stdout/stderr
                # handles pass through to the child.
                $launcher = Join-Path $script:Root ("config\launch-{0}.ps1" -f $name)
                $lines = @('$ErrorActionPreference = "Stop"')
                foreach ($key in $environment.Keys) {
                    $value = ([string]$environment[$key]).Replace("'", "''")
                    $lines += ('$env:{0} = ''{1}''' -f $key, $value)
                }
                $escapedFile = $filePath.Replace("'", "''")
                $argList = @()
                foreach ($argument in $arguments) { $argList += ("'" + $argument.Replace("'", "''") + "'") }
                # Invoking a GUI-subsystem exe (pythonw.exe) with the & operator does
                # NOT block — PowerShell starts it asynchronously and the launcher
                # would exit, orphaning the service and leaving a dead tracked PID.
                # Start-Process -PassThru keeps the launcher alive as the real parent
                # so Stop-NativeProcessTree can still tear the tree down. Redirect the
                # inner process's stdout/stderr to dedicated files too: pythonw has no
                # console, so without a redirect the service's output vanishes (run8's
                # api failed with zero output anywhere, hiding the failure entirely).
                $innerOut = Join-Path $script:Root ("logs\{0}.inner.out.log" -f $name)
                $innerErr = Join-Path $script:Root ("logs\{0}.inner.err.log" -f $name)
                if ($argList.Count -gt 0) {
                    $lines += ("`$p = Start-Process -FilePath '{0}' -ArgumentList {1} -RedirectStandardOutput '{2}' -RedirectStandardError '{3}' -PassThru -WindowStyle Hidden" -f $escapedFile, ($argList -join ", "), $innerOut.Replace("'", "''"), $innerErr.Replace("'", "''"))
                } else {
                    $lines += ("`$p = Start-Process -FilePath '{0}' -RedirectStandardOutput '{1}' -RedirectStandardError '{2}' -PassThru -WindowStyle Hidden" -f $escapedFile, $innerOut.Replace("'", "''"), $innerErr.Replace("'", "''"))
                }
                $lines += "`$p.WaitForExit()"
                Write-AtomicText $launcher ($lines -join "`r`n")
                $startOptions.FilePath = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
                $startOptions.ArgumentList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $launcher)
            }
        }
        $process = Start-Process @startOptions
    } finally {
        foreach ($key in $environment.Keys) {
            if ($null -eq $old[$key]) { Remove-Item -Path ("Env:{0}" -f $key) -ErrorAction SilentlyContinue }
            else { Set-Item -Path ("Env:{0}" -f $key) -Value $old[$key] }
        }
    }
    return [pscustomobject]@{
        name = $name
        role = $role
        pid = $process.Id
        embedded_in = $embeddedIn
        started_at = [DateTime]::UtcNow.ToString("o")
    }
}

function Wait-NativeHttp([string]$uri) {
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri $uri
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Seconds 1
    }
    throw "native endpoint did not become ready: $uri"
}

function Wait-NativeStartupReady {
    # The watchdog task brings the stack up asynchronously after
    # Start-ScheduledTask; the first loop can take a while because it runs
    # migrations and waits for PostgreSQL. Poll until the tracked services are
    # alive and the API answers, mirroring Invoke-StartCore's readiness checks.
    # Load the configured ports first: the api may be on a Reconcile-shifted port.
    [void](Get-NativePortsFromFile)
    $deadline = [DateTime]::UtcNow.AddSeconds(180)
    while ([DateTime]::UtcNow -lt $deadline) {
        $services = @(Read-State)
        $managed = @($services | Where-Object { -not $_.embedded_in })
        $allAlive = $managed.Count -gt 0 -and @($managed | Where-Object { -not (Test-ManagedProcess $_) }).Count -eq 0
        if ($allAlive) {
            try {
                $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri ("http://127.0.0.1:{0}/api/v1/health" -f $script:ApiPort)
                if ($response.StatusCode -eq 200) { return }
            } catch { }
        }
        Start-Sleep -Seconds 5
    }
    throw "native runtime did not become ready within 180 seconds; the watchdog task may have failed — run status to diagnose"
}

function Test-ManagedProcess($service) {
    return $service.pid -and (Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue)
}

function Wait-PostgresReady([string]$pgIsReady) {
    $oldEap = $ErrorActionPreference
    try {
        # pg_isready exits 2 (with text on stdout) while Postgres is still booting;
        # native stderr would otherwise raise a terminating NativeCommandError here.
        $ErrorActionPreference = "Continue"
        for ($attempt = 1; $attempt -le 30; $attempt++) {
            & $pgIsReady -h 127.0.0.1 -p $script:PostgresPort -U ashare 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return
            }
            Start-Sleep -Seconds 1
        }
    } finally {
        $ErrorActionPreference = $oldEap
    }
    throw "PostgreSQL did not become ready on port $script:PostgresPort"
}

function Wait-RedisReady([string]$redisCli, [string]$password) {
    $oldRedisAuth = $env:REDISCLI_AUTH
    $oldEap = $ErrorActionPreference
    try {
        # Connection-refused writes to redis-cli stderr; under EAP=Stop that becomes
        # a terminating NativeCommandError (even with 2>$null) and kills the retry
        # loop on the first attempt while Redis is still starting.
        $ErrorActionPreference = "Continue"
        $env:REDISCLI_AUTH = $password
        for ($attempt = 1; $attempt -le 30; $attempt++) {
            $reply = & $redisCli -h 127.0.0.1 -p $script:RedisPort ping 2>$null
            if ($LASTEXITCODE -eq 0 -and "$reply".Trim() -eq "PONG") {
                return
            }
            Start-Sleep -Seconds 1
        }
    } finally {
        $ErrorActionPreference = $oldEap
        if ($null -eq $oldRedisAuth) { Remove-Item Env:REDISCLI_AUTH -ErrorAction SilentlyContinue }
        else { $env:REDISCLI_AUTH = $oldRedisAuth }
    }
    throw "Redis did not become ready on port $script:RedisPort"
}

function Invoke-Install {
    Assert-ExternalRoot
    Initialize-Directories
    $runningBeforeInstall = @(Read-State | Where-Object { Test-ManagedProcess $_ })
    if ($runningBeforeInstall.Count -gt 0) {
        throw "native runtime is running; stop it before install or repair"
    }
    Set-NativeDesiredState "STOPPED"
    Initialize-NativePorts
    $manifest = Read-Manifest
    if ($manifest.platform -ne "windows-amd64") { throw "unsupported native dependency platform" }
    $postgres = $manifest.artifacts | Where-Object id -eq "postgres"
    $redis = $manifest.artifacts | Where-Object id -eq "redis-compatible"
    $searxng = $manifest.artifacts | Where-Object id -eq "searxng"
    $postgresArchive = Get-VerifiedArchive $postgres
    $redisArchive = Get-VerifiedArchive $redis
    $postgresRoot = Expand-VerifiedArtifact $postgres $postgresArchive
    $redisRoot = Expand-VerifiedArtifact $redis $redisArchive
    $postgresBin = Split-Path -Parent (Find-Executable $postgresRoot "pg_ctl.exe")
    $redisBin = Split-Path -Parent (Find-Executable $redisRoot "redis-server.exe")
    foreach ($executable in @("postgres.exe", "pg_isready.exe", "createdb.exe", "psql.exe")) {
        if (-not (Test-Path -LiteralPath (Join-Path $postgresBin $executable) -PathType Leaf)) {
            throw "$executable is missing from the PostgreSQL runtime"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $redisBin "redis-cli.exe") -PathType Leaf)) {
        throw "redis-cli.exe is missing from the Redis-compatible runtime"
    }
    Copy-Item -Recurse -Force (Join-Path $script:SourceRoot "migrations\*") (Join-Path $script:Root "migrations")
    Copy-Item -Recurse -Force (Join-Path $script:SourceRoot "configs\*") (Join-Path $script:Root "configs")
    Copy-Item -Force (Join-Path $script:SourceRoot "alembic.ini") (Join-Path $script:Root "alembic.ini")
    $bundledPythonInstaller = Join-Path $script:SourceRoot "vendor\python-3.12.10-amd64.exe"
    $managedPython = Join-Path $script:Root "tools\python\python.exe"
    $pythonCommand = if (Test-Path -LiteralPath $bundledPythonInstaller -PathType Leaf) { $null } else { Get-Command python.exe -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $managedPython -PathType Leaf) {
        $pythonCommand = Get-Item -LiteralPath $managedPython
    }
    if (-not $pythonCommand -and (Test-Path -LiteralPath $bundledPythonInstaller -PathType Leaf)) {
        $expectedPythonHash = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
        $actualPythonHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $bundledPythonInstaller).Hash
        if (-not $actualPythonHash.Equals($expectedPythonHash, [StringComparison]::OrdinalIgnoreCase)) { throw "bundled Python checksum mismatch" }
        $pythonRoot = Join-Path $script:Root "tools\python"
        New-Item -ItemType Directory -Force -Path $pythonRoot | Out-Null
        $pythonInstall = Start-Process -FilePath $bundledPythonInstaller -ArgumentList @(
            "/quiet", "InstallAllUsers=0", "TargetDir=$pythonRoot", "Include_pip=1", "Include_launcher=0",
            "Include_test=0", "Shortcuts=0", "AssociateFiles=0", "PrependPath=0"
        ) -Wait -PassThru
        if ($pythonInstall.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $managedPython -PathType Leaf)) { throw "bundled Python installation failed with exit code $($pythonInstall.ExitCode)" }
        $pythonCommand = Get-Item -LiteralPath $managedPython
    }
    if (-not $pythonCommand) { throw "Python 3.11 or 3.12 is required for native installation" }
    $pythonExecutablePath = if ($pythonCommand.Source) { [string]$pythonCommand.Source } else { [string]$pythonCommand.FullName }
    $pythonVersion = (& $pythonExecutablePath -c "import sys; print('.'.join(map(str, sys.version_info[:2])))").Trim()
    if ($pythonVersion -notin @("3.11", "3.12")) { throw "Python 3.11 or 3.12 is required, found $pythonVersion" }
    $pythonwPath = Join-Path (Split-Path -Parent $pythonExecutablePath) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
        throw "pythonw.exe is missing from the host Python runtime"
    }
    $venvPython = Join-Path $script:Root "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        & $pythonExecutablePath -m venv (Join-Path $script:Root "venv")
        if ($LASTEXITCODE -ne 0) { throw "could not create native Python environment" }
    } else {
        & $venvPython --version 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "existing native Python environment is not usable" }
    }
    & $venvPython -m pip install --requirement (Join-Path $script:SourceRoot "requirements.runtime.lock")
    if ($LASTEXITCODE -ne 0) { throw "native Python dependency installation failed" }
    & $venvPython -m pip install --no-deps $script:SourceRoot
    if ($LASTEXITCODE -ne 0) { throw "native application installation failed" }
    $searxngRoot = Install-Searxng $searxng
    $gitConfigPath = Join-Path $script:Root "config\gitconfig"
    @"
[safe]
    directory = $($searxngRoot.Replace('\', '/'))
"@ | Set-Content -LiteralPath $gitConfigPath -Encoding ASCII
    $searxRequirements = Get-ChildItem -LiteralPath $searxngRoot -File -Filter "requirements*.txt" -ErrorAction SilentlyContinue
    foreach ($requirements in $searxRequirements) {
        & $venvPython -m pip install --requirement $requirements.FullName
        if ($LASTEXITCODE -ne 0) { throw "SearXNG dependency installation failed: $($requirements.Name)" }
    }
    $searxngImportPath = $searxngRoot.Replace("\", "/")
    & $venvPython -c "import sys; sys.path.insert(0, '$searxngImportPath'); import searx"
    if ($LASTEXITCODE -ne 0) { throw "SearXNG source import validation failed" }
    $prebuiltWeb = Join-Path $script:SourceRoot "web\dist"
    if (Test-Path -LiteralPath (Join-Path $prebuiltWeb "index.html") -PathType Leaf) {
        $runtimeWeb = Join-Path $script:Root "web"
        if (Test-Path -LiteralPath $runtimeWeb) { Remove-Item -Recurse -Force -LiteralPath $runtimeWeb }
        Copy-Item -Recurse -Force -LiteralPath $prebuiltWeb -Destination $runtimeWeb
    } else {
    $npmCommand = Get-Command npm.cmd, npm.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $npmPath = if ($npmCommand) {
        $npmCommand.Source
    } else {
        @(
            (Join-Path $env:ProgramFiles "nodejs\npm.cmd"),
            (Join-Path $env:LOCALAPPDATA "Programs\nodejs\npm.cmd")
        ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    if (-not $npmPath) { throw "Node.js 20+ is required to build the native Web assets" }
    $webSource = Join-Path $script:Root "build\web-source"
    if (Test-Path -LiteralPath $webSource) { Remove-Item -Recurse -Force -LiteralPath $webSource }
    Copy-Item -Recurse -Force (Join-Path $script:SourceRoot "web") $webSource
    Push-Location $webSource
    try {
        & $npmPath ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw "native Web dependency installation failed" }
        & $npmPath run build -- --outDir (Join-Path $script:Root "web")
        if ($LASTEXITCODE -ne 0) { throw "native Web build failed" }
    } finally { Pop-Location }
    }
    $postgresPassword = if (Test-Path (Join-Path $script:Root "config\postgres-password.txt")) { (Get-Content (Join-Path $script:Root "config\postgres-password.txt") -Raw).Trim() } else { New-RandomHex }
    $redisPassword = if (Test-Path (Join-Path $script:Root "config\redis-password.txt")) { (Get-Content (Join-Path $script:Root "config\redis-password.txt") -Raw).Trim() } else { New-RandomHex }
    $adminPasswordValue = if ($AdminPassword) { $AdminPassword } elseif (Test-Path (Join-Path $script:Root "config\admin-credentials.txt")) { ((Get-Content (Join-Path $script:Root "config\admin-credentials.txt") | Where-Object { $_ -like "password=*" }) -replace "^password=", "") } else { New-RandomHex }
    $fernetKey = if (Test-Path $script:EnvPath) { ((Get-Content $script:EnvPath | Where-Object { $_ -like "MODEL_SETTINGS_ENCRYPTION_KEYS=*" }) -replace "^MODEL_SETTINGS_ENCRYPTION_KEYS=", "") } else { ConvertTo-FernetKey }
    Write-NativeEnv $postgresPassword $redisPassword $adminPasswordValue $fernetKey
    $pythonSitePackages = Join-Path $script:Root "venv\Lib\site-packages"
    Write-AtomicText (Join-Path $script:Root "config\native-paths.json") (([ordered]@{
        postgres_bin = $postgresBin
        redis_bin = $redisBin
        searxng_root = $searxngRoot
        python_exe = $pythonExecutablePath
        pythonw_exe = $pythonwPath
        python_site_packages = $pythonSitePackages
        postgres_port = $script:PostgresPort
        redis_port = $script:RedisPort
        api_port = $script:ApiPort
        searxng_port = $script:SearxngPort
        version = $script:NativeVersion
    } | ConvertTo-Json -Depth 8))
    Write-NativePortConfig
    Apply-NativeIdentity (Split-Path -Parent $pythonExecutablePath)
    $secretFile = Join-Path $script:Root "config\admin-credentials.txt"
    Write-Host "Native installation is ready at $script:Root"
    Write-Host "The generated administrator credentials are stored in $secretFile"
    Write-Host "Native services and the watchdog run as the built-in NETWORK SERVICE account, no extra account"
    Write-Host "Watchdog task: $script:WatchdogTaskName"
}

function Invoke-StartCore([switch]$ForWatchdog) {
    Assert-ExternalRoot
    if (-not (Test-Path -LiteralPath $script:EnvPath -PathType Leaf)) { throw "native .env is missing; run install first" }
    $pathsFile = Join-Path $script:Root "config\native-paths.json"
    if (-not (Test-Path -LiteralPath $pathsFile -PathType Leaf)) { throw "native paths are missing; run install first" }
    $paths = Get-Content -Raw -LiteralPath $pathsFile | ConvertFrom-Json
    # native-ports.json is the single authoritative port store (Write-NativePortConfig
    # keeps native-paths.json's *_port fields in sync, but StartCore must not read
    # them back — a stale paths snapshot would pin the api to a drifted port).
    if (-not (Get-NativePortsFromFile)) {
        throw "native port configuration is missing; run install first"
    }
    $pythonExecutable = if ($paths.python_exe -and (Test-Path -LiteralPath $paths.python_exe -PathType Leaf)) {
        [string]$paths.python_exe
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonCommand) { $pythonCommand.Source } else { Join-Path $script:Root "venv\Scripts\python.exe" }
    }
    $pythonSitePackages = if ($paths.python_site_packages) {
        [string]$paths.python_site_packages
    } else {
        Join-Path $script:Root "venv\Lib\site-packages"
    }
    if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
        throw "native Python executable is missing; run install first"
    }
    if (-not (Test-Path -LiteralPath $pythonSitePackages -PathType Container)) {
        throw "native Python site-packages are missing; run install first"
    }
    $pythonWindowlessExecutable = if ($paths.pythonw_exe -and (Test-Path -LiteralPath $paths.pythonw_exe -PathType Leaf)) {
        [string]$paths.pythonw_exe
    } else {
        $candidate = Join-Path (Split-Path -Parent $pythonExecutable) "pythonw.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate } else { $pythonExecutable }
    }
    $state = @(Read-State)
    $running = @($state | Where-Object { Test-ManagedProcess $_ })
    if ($state.Count -gt 0 -and $running.Count -eq $state.Count) {
        Write-Host "Native runtime is already running"
        return
    }
    if ($running.Count -gt 0) {
        throw "Native runtime has only some services running; stop it before starting again"
    }
    Write-NativeEvent "starting native process group"
    Reconcile-NativePorts
    $paths = Get-Content -Raw -LiteralPath $pathsFile | ConvertFrom-Json
    # account mode launches every service with -Credential (independent of the
    # caller's identity); task mode inherits the watchdog's identity.
    $serviceCredential = if ((Get-NativeIdentityMode) -eq "account") { Read-NativeServiceCredential } else { $null }
    $envValues = @{}
    Get-Content -LiteralPath $script:EnvPath | Where-Object { $_ -match "^(?<key>[A-Z0-9_]+)=(?<value>.*)$" } | ForEach-Object { $envValues[$Matches.key] = $Matches.value }
    # The Windows venv launcher keeps a second process alive. Long-lived native
    # services use the installed base interpreter with the venv packages added
    # explicitly; sys.executable then preserves the isolated market subprocess.
    $envValues.PYTHONPATH = if ($envValues.PYTHONPATH) {
        "$pythonSitePackages;$($envValues.PYTHONPATH)"
    } else {
        $pythonSitePackages
    }
    Set-NativeProcessEnvironment $envValues
    Push-Location $script:Root
    try {
        $services = @()
        $pgData = Join-Path $script:Root "data\postgres"
        $initdb = Join-Path $paths.postgres_bin "initdb.exe"
        $postgres = Join-Path $paths.postgres_bin "postgres.exe"
        $pgIsReady = Join-Path $paths.postgres_bin "pg_isready.exe"
        if (-not (Test-Path (Join-Path $pgData "PG_VERSION"))) {
            $pgPasswordFile = Join-Path $script:Root "config\postgres-password.txt"
            $oldInitdbEap = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                & $initdb -D $pgData -U ashare --pwfile=$pgPasswordFile --encoding=UTF8 --locale=C
            } finally {
                $ErrorActionPreference = $oldInitdbEap
            }
            if ($LASTEXITCODE -ne 0) { throw "PostgreSQL initdb failed" }
        }
        if (-not (Test-Path -LiteralPath $postgres -PathType Leaf)) { throw "postgres.exe is missing" }
        if (-not (Test-Path -LiteralPath $pgIsReady -PathType Leaf)) { throw "pg_isready.exe is missing" }
        $postgresArguments = @(
            "-D", $pgData,
            "-p", "$script:PostgresPort",
            "-c", "shared_buffers=32MB",
            "-c", "max_connections=32",
            "-c", "work_mem=4MB",
            "-c", "maintenance_work_mem=16MB",
            "-c", "effective_cache_size=128MB"
        )
        Assert-NativePortFree $script:PostgresPort
        $services += Start-ManagedProcess "postgres" $postgres $postgresArguments $script:Root @{} "postgres" $null $serviceCredential
        Write-NativeEvent "postgres process started pid=$($services[-1].pid)"
        Wait-PostgresReady $pgIsReady
        Assert-NativePortOwned $script:PostgresPort $services
        $createdb = Join-Path $paths.postgres_bin "createdb.exe"
        $psql = Join-Path $paths.postgres_bin "psql.exe"
        $oldPgPassword = $env:PGPASSWORD
        $oldPgEap = $ErrorActionPreference
        try {
            # createdb reports "database already exists" on stderr; under EAP=Stop
            # that is a terminating NativeCommandError that would skip the psql
            # fallback below even though $LASTEXITCODE is the intended signal.
            $ErrorActionPreference = "Continue"
            $env:PGPASSWORD = (Get-Content (Join-Path $script:Root "config\postgres-password.txt") -Raw).Trim()
            & $createdb -h 127.0.0.1 -p $script:PostgresPort -U ashare ashare 2>$null
            $createdbExit = $LASTEXITCODE
            if ($createdbExit -ne 0) {
                & $psql -h 127.0.0.1 -p $script:PostgresPort -U ashare -d ashare -c "SELECT 1" 2>$null | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "PostgreSQL database creation failed" }
            }
        } finally {
            $ErrorActionPreference = $oldPgEap
            if ($null -eq $oldPgPassword) { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
            else { $env:PGPASSWORD = $oldPgPassword }
        }
        New-Item -ItemType File -Force -Path (Join-Path $script:Root "state\database-ready") | Out-Null
        $redisConfig = Join-Path $script:Root "config\redis.conf"
        $redisPassword = (Get-Content (Join-Path $script:Root "config\redis-password.txt") -Raw).Trim()
        $redisCli = Join-Path $paths.redis_bin "redis-cli.exe"
        $redisConfLines = @(
            "bind 127.0.0.1",
            "port $script:RedisPort",
            "protected-mode yes",
            "requirepass $redisPassword",
            "dir `"$($script:Root.Replace('\', '/'))/data/redis`"",
            "appendonly yes",
            "appendfsync everysec"
        )
        # WriteAllLines emits UTF-8 without BOM; Set-Content -Encoding UTF8 would
        # prepend a BOM and Redis would reject the first directive ("unknown conf
        # file parameter : bind") and exit immediately.
        [System.IO.File]::WriteAllLines($redisConfig, $redisConfLines, [System.Text.UTF8Encoding]::new($false))
        Assert-NativePortFree $script:RedisPort
        $services += Start-ManagedProcess "redis" (Join-Path $paths.redis_bin "redis-server.exe") @($redisConfig) $script:Root @{} "redis" $null $serviceCredential
        Write-NativeEvent "redis process started pid=$($services[-1].pid)"
        Wait-RedisReady $redisCli $redisPassword
        Assert-NativePortOwned $script:RedisPort $services
        Invoke-NativePython @("-m", "ashare_ai.cli", "migrate")
        Write-NativeSearxSettings $redisPassword
        $searxEnv = @{}
        foreach ($key in $envValues.Keys) { $searxEnv[$key] = $envValues[$key] }
        $searxEnv.SEARXNG_SETTINGS_PATH = (Join-Path $script:Root "config\searxng-settings.yml")
        $searxEnv.SEARXNG_BASE_URL = "http://127.0.0.1:$script:SearxngPort/"
        $searxEnv.GIT_CONFIG_GLOBAL = (Join-Path $script:Root "config\gitconfig")
        $searxPython = $pythonWindowlessExecutable
        Assert-NativePortFree $script:SearxngPort
        $services += Start-ManagedProcess "searxng" $searxPython @("-m", "searx.webapp") $paths.searxng_root $searxEnv "searxng" $null $serviceCredential
        Write-NativeEvent "searxng process started pid=$($services[-1].pid)"
        Wait-NativeHttp "http://127.0.0.1:$script:SearxngPort/healthz"
        Assert-NativePortOwned $script:SearxngPort $services
        $apiEnv = @{}
        foreach ($key in $envValues.Keys) { $apiEnv[$key] = $envValues[$key] }
        $apiEnv.ASHARE_NATIVE_WEB_ROOT = (Join-Path $script:Root "web")
        $apiEnv.EDGE_GATEWAY_SOURCE_DIR = (Join-Path $script:Root "config\edge-gateway")
        $apiEnv.EDGE_GATEWAY_HOST_SOURCE_DIR = (Join-Path $script:Root "config\edge-gateway")
        $apiEnv.EDGE_GATEWAY_CONFIG_DIR = (Join-Path $script:Root "config\edge-gateway")
        $apiEnv.EDGE_GATEWAY_LOG_DIR = (Join-Path $script:Root "logs\edge-gateway")
        New-Item -ItemType Directory -Force -Path $apiEnv.EDGE_GATEWAY_SOURCE_DIR | Out-Null
        New-Item -ItemType Directory -Force -Path $apiEnv.EDGE_GATEWAY_LOG_DIR | Out-Null
        Assert-NativePortFree $script:ApiPort
        $services += Start-ManagedProcess "api" $searxPython @("-m", "ashare_ai.cli", "api", "--host", "127.0.0.1", "--port", "$script:ApiPort") $script:Root $apiEnv "api" $null $serviceCredential
        Write-NativeEvent "api process started pid=$($services[-1].pid)"
        Wait-NativeHttp "http://127.0.0.1:$script:ApiPort/api/v1/health"
        Assert-NativePortOwned $script:ApiPort $services
        $services += [pscustomobject]@{ name = "web"; role = "web"; pid = $services[-1].pid; embedded_in = "api"; started_at = [DateTime]::UtcNow.ToString("o") }
        $services += Start-ManagedProcess "job-worker" $searxPython @("-m", "ashare_ai.orchestration.serial_worker") $script:Root $envValues "job-worker" $null $serviceCredential
        $services += Start-ManagedProcess "exit-advice-worker" $searxPython @("-m", "ashare_ai.orchestration.exit_advice_worker") $script:Root $envValues "exit-advice-worker" $null $serviceCredential
        if ($ResearchMode -eq "DUAL" -and $ResearchWorkers -gt 0) {
            for ($index = 1; $index -le $ResearchWorkers; $index++) {
                $services += Start-ManagedProcess ("research-worker-{0}" -f $index) $searxPython @("-m", "ashare_ai.orchestration.research_worker") $script:Root $envValues "research-worker" $null $serviceCredential
            }
        }
        Write-State $services
        Write-NativeEvent "native process group is healthy"
    } catch {
        foreach ($service in @($services | Sort-Object @{ Expression = { if ($_.role -eq "postgres") { 0 } else { 1 } } })) {
            if ($service.embedded_in) { continue }
            if ($service.role -eq "postgres") {
                $pgCtl = Join-Path $paths.postgres_bin "pg_ctl.exe"
                $pgData = Join-Path $script:Root "data\postgres"
                $oldStopEap = $ErrorActionPreference
                try {
                    $ErrorActionPreference = "Continue"
                    & $pgCtl -D $pgData -m immediate stop 2>$null | Out-Null
                } finally {
                    $ErrorActionPreference = $oldStopEap
                }
            }
            # Teardown the tracked service process tree: Stop-NativeProcessTree
            # walks Win32_Process children from the tracked pid, so killing the
            # root also reaps any backends the service spawned.
            Stop-NativeProcessTree ([int]$service.pid)
        }
        Write-State @()
        throw
    } finally { Pop-Location }
    Write-Host "Native API/Web: http://127.0.0.1:$script:ApiPort/"
}

function Invoke-StopCore {
    $services = @(Read-State)
    $pathsFile = Join-Path $script:Root "config\native-paths.json"
    $paths = if (Test-Path -LiteralPath $pathsFile -PathType Leaf) {
        Get-Content -Raw -LiteralPath $pathsFile | ConvertFrom-Json
    } else { $null }
    foreach ($service in @($services | Sort-Object @{ Expression = { if ($_.role -eq "postgres") { 0 } else { 1 } } })) {
        if ($service.embedded_in) { continue }
        if ($service.role -eq "postgres" -and $null -ne $paths) {
            $pgCtl = Join-Path $paths.postgres_bin "pg_ctl.exe"
            $pgData = Join-Path $script:Root "data\postgres"
            $oldStopEap = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                & $pgCtl -D $pgData -m fast stop 2>$null | Out-Null
            } finally {
                $ErrorActionPreference = $oldStopEap
            }
            if ($LASTEXITCODE -eq 0) { Write-Host ("Stopped {0} (graceful)" -f $service.name) }
        }
        $process = Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue
        if ($process -or $service.role -eq "postgres") {
            Stop-NativeProcessTree ([int]$service.pid)
            Write-Host ("Stopped {0} (PID {1})" -f $service.name, $service.pid)
        }
    }
    Write-State @()
    Remove-Item -Path (Join-Path $script:Root "config\launch-*.ps1") -Force -ErrorAction SilentlyContinue
    Write-NativeEvent "native runtime stopped"
}

function Invoke-Start([switch]$ForWatchdog) {
    $mutex = Enter-NativeManagementLock
    try {
        if (-not $ForWatchdog) {
            # Reconcile the account/task/ACL to the mode chosen in the web
            # settings, so a mode change takes effect on this start.
            Apply-NativeIdentity
        }
        Set-NativeDesiredState "RUNNING"
        Write-NativeEvent "native runtime start requested"
        if ($ForWatchdog) {
            # Only the watchdog (running as the mode's task identity) may launch
            # the service processes for task mode. An interactive shell
            # is typically elevated, and PostgreSQL refuses to boot under an
            # administrative token, so the inline start must never run from the
            # caller's identity.
            Invoke-StartCore -ForWatchdog:$true
        } elseif ((Get-NativeIdentityMode) -eq "account") {
            # account mode launches every service with -Credential, so the
            # caller's identity never leaks into the services; inline start is fine.
            Invoke-StartCore -ForWatchdog:$false
        }
    } finally {
        Exit-NativeMutex $mutex
    }
    if (-not $ForWatchdog) {
        # Flip desired state and let the watchdog task bring the stack up (or
        # start services inline in account mode), then wait for readiness.
        Start-NativeWatchdogTask
        if ((Get-NativeIdentityMode) -ne "account") {
            Wait-NativeStartupReady
        }
    }
}

function Invoke-Stop([switch]$ForRecovery) {
    $mutex = Enter-NativeManagementLock
    try {
        if (-not $ForRecovery) {
            Set-NativeDesiredState "STOPPED"
            Stop-NativeWatchdogTask
        }
        Invoke-StopCore
    } finally {
        Exit-NativeMutex $mutex
    }
}

function Invoke-Restart {
    Invoke-Stop
    Invoke-Start
}

function Invoke-Repair {
    $mutex = Enter-NativeManagementLock
    try {
        $running = @(Read-State | Where-Object { Test-ManagedProcess $_ })
        if ($running.Count -gt 0) { throw "native runtime is running; stop it before repair" }
        Set-NativeDesiredState "STOPPED"
        if (-not (Get-NativePortsFromFile)) { throw "native port configuration is missing; run install first" }
        Reconcile-NativePorts
        Apply-NativeIdentity
        Write-NativeEvent "native runtime configuration repaired"
        Write-Host "Native runtime management is repaired"
    } finally {
        Exit-NativeMutex $mutex
    }
}

function Invoke-Watchdog {
    Assert-ExternalRoot
    # No Ensure-NativeWatchdogTask here: the watchdog was launched BY the task,
    # so the task is registered by definition, and re-registering at runtime would
    # fail for account mode (the service account cannot write to the Task Scheduler
    # store). Registration/application happens on elevated install/start/repair.
    $mutex = New-NativeMutex $script:WatchdogMutexName
    try {
        try {
            if (-not $mutex.WaitOne(0)) {
                Write-NativeEvent "duplicate watchdog exited"
                return
            }
        } catch [Threading.AbandonedMutexException] {
            # The previous watchdog exited while holding the mutex; continue as owner.
        }
        $restartCount = 0
        $backoff = 5
        Write-NativeWatchdogState @{ status = "RUNNING"; restart_count = 0; last_check_at = [DateTime]::UtcNow.ToString("o"); last_error = $null }
        Write-NativeEvent "watchdog loop started"
        while ((Get-NativeDesiredState) -eq "RUNNING") {
            $services = @(Read-State)
            $healthy = Test-NativeRuntimeHealthy $services
            $now = [DateTime]::UtcNow.ToString("o")
            if ($healthy) {
                $backoff = 5
                Write-NativeWatchdogState @{ status = "HEALTHY"; restart_count = $restartCount; last_check_at = $now; last_error = $null }
            } else {
                $restartCount++
                $message = "runtime health check failed; recovery attempt $restartCount"
                Write-NativeEvent $message "WARN"
                Write-NativeWatchdogState @{ status = "RECOVERING"; restart_count = $restartCount; last_check_at = $now; last_error = $message }
                try {
                    Invoke-Stop -ForRecovery
                    Start-Sleep -Seconds 2
                    Invoke-Start -ForWatchdog
                    Write-NativeEvent "watchdog recovery completed"
                    Write-NativeWatchdogState @{ status = "HEALTHY"; restart_count = $restartCount; last_check_at = [DateTime]::UtcNow.ToString("o"); last_error = $null }
                    $backoff = 5
                } catch {
                    $errorText = $_.Exception.Message
                    Write-NativeEvent "watchdog recovery failed: $errorText" "ERROR"
                    Write-NativeWatchdogState @{ status = "BACKOFF"; restart_count = $restartCount; last_check_at = [DateTime]::UtcNow.ToString("o"); last_error = $errorText }
                    Start-Sleep -Seconds $backoff
                    $backoff = [math]::Min(60, $backoff * 2)
                }
            }
            Start-Sleep -Seconds $WatchdogIntervalSeconds
        }
        Write-NativeEvent "watchdog loop stopped because desired state is STOPPED"
    } finally {
        Write-NativeWatchdogState @{ status = "STOPPED"; last_check_at = [DateTime]::UtcNow.ToString("o") }
        try { $mutex.ReleaseMutex() } catch { }
        $mutex.Dispose()
    }
}

function Invoke-Status {
    [void](Get-NativePortsFromFile)
    $services = @(Read-State)
    $report = Get-WorkingSetReport $services -Fast:$Fast
    $task = Get-NativeTaskSummary -Fast:$Fast
    $watchdog = Read-NativeWatchdogState
    $runtimeHealthy = if ($services.Count -gt 0) {
        if ($Fast) { Test-NativeRuntimeHealthyFast $report.services } else { Test-NativeRuntimeHealthy $services }
    } else { $false }
    $installation = Get-NativeInstallationState
    $report | Add-Member -NotePropertyName desired_state -NotePropertyValue (Get-NativeDesiredState)
    $report | Add-Member -NotePropertyName runtime_healthy -NotePropertyValue $runtimeHealthy
    $report | Add-Member -NotePropertyName ports -NotePropertyValue ([ordered]@{
        postgres = $script:PostgresPort
        redis = $script:RedisPort
        api = $script:ApiPort
        searxng = $script:SearxngPort
    })
    $report | Add-Member -NotePropertyName watchdog_task -NotePropertyValue $task
    $report | Add-Member -NotePropertyName watchdog -NotePropertyValue $watchdog
    $report | Add-Member -NotePropertyName installation -NotePropertyValue $installation
    $report | Add-Member -NotePropertyName identity_mode -NotePropertyValue (Get-NativeIdentityMode)
    if ($Json) { $report | ConvertTo-Json -Depth 10 -Compress:$Fast; return }
    if ($services.Count -eq 0) { Write-Host "Native runtime is stopped"; return }
    $report.services | Format-Table service, role, pid, healthy, working_set_mib, embedded_in -AutoSize
    Write-Host ("Desired state: {0}; health: {1}" -f $report.desired_state, $report.runtime_healthy)
    Write-Host ("Ports: postgres={0}, redis={1}, api={2}, searxng={3}" -f $script:PostgresPort, $script:RedisPort, $script:ApiPort, $script:SearxngPort)
    $watchdogStatus = if ($watchdog) { $watchdog.status } else { "MISSING" }
    Write-Host ("Watchdog task: {0} ({1}); process: {2}" -f $task.task_name, $task.state, $watchdogStatus)
    Write-Host ("Identity mode: {0}" -f $report.identity_mode)
    Write-Host ("Process-group working set: {0} MiB" -f $report.total_working_set_mib)
}

function Invoke-Doctor {
    Assert-ExternalRoot
    $checks = @()
    $checks += [pscustomobject]@{ check = "runtime-root-outside-source"; status = if ($script:Root -notlike "$script:SourceRoot*") { "PASS" } else { "FAIL" }; detail = $script:Root }
    $checks += [pscustomobject]@{ check = "native-env"; status = if (Test-Path $script:EnvPath) { "PASS" } else { "FAIL" }; detail = $script:EnvPath }
    $checks += [pscustomobject]@{ check = "python"; status = if (Test-Path (Join-Path $script:Root "venv\Scripts\python.exe")) { "PASS" } else { "FAIL" }; detail = "venv" }
    $checks += [pscustomobject]@{ check = "web-index"; status = if (Test-Path (Join-Path $script:Root "web\index.html")) { "PASS" } else { "FAIL" }; detail = "static SPA" }
    $task = Get-NativeTaskSummary
    $checks += [pscustomobject]@{ check = "watchdog-task"; status = if ($task.registered) { "PASS" } else { "FAIL" }; detail = $task.task_name }
    $checks += [pscustomobject]@{ check = "desired-state"; status = if ((Get-NativeDesiredState) -in @("RUNNING", "STOPPED")) { "PASS" } else { "FAIL" }; detail = (Get-NativeDesiredState) }
    $portsReady = Get-NativePortsFromFile
    $portValues = @($script:PostgresPort, $script:RedisPort, $script:ApiPort, $script:SearxngPort)
    $checks += [pscustomobject]@{ check = "port-configuration"; status = if ($portsReady -and (@($portValues | Select-Object -Unique).Count -eq 4)) { "PASS" } else { "FAIL" }; detail = ($portValues -join ",") }
    $checks += [pscustomobject]@{ check = "docker-wsl-processes"; status = if (@(Get-Process -Name "docker*","wsl*" -ErrorAction SilentlyContinue).Count -eq 0) { "PASS" } else { "WARN" }; detail = "native entry does not start Docker or WSL" }
    if ($Json) { $checks | ConvertTo-Json -Depth 5; return }
    $checks | Format-Table check, status, detail -AutoSize
    if (@($checks | Where-Object status -eq "FAIL").Count -gt 0) { exit 1 }
}

switch ($Command) {
    "install" { Invoke-Install }
    "start" { Invoke-Start }
    "stop" { Invoke-Stop }
    "restart" { Invoke-Restart }
    "repair" { Invoke-Repair }
    "status" { Invoke-Status; exit 0 }
    "doctor" { Invoke-Doctor }
    "watchdog" { Invoke-Watchdog }
}
