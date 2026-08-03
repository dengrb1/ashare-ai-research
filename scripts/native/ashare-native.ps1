[CmdletBinding()]
param(
    [ValidateSet("install", "start", "stop", "status", "doctor")]
    [string]$Command = "status",
    [string]$Root,
    [string]$SourceRoot,
    [ValidateSet("SERIAL", "DUAL")]
    [string]$ResearchMode = "SERIAL",
    [ValidateRange(0, 2)]
    [int]$ResearchWorkers = 0,
    [switch]$Json,
    [string]$AdminUsername = "admin",
    [string]$AdminPassword
)

$ErrorActionPreference = "Stop"
$script:NativeVersion = "2026.07.31"
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
$script:EnvPath = Join-Path $script:Root ".env"
$script:PortsPath = Join-Path $script:Root "config\native-ports.json"
$script:ManifestPath = Join-Path $script:ScriptRoot "dependencies.lock.json"

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

function Initialize-NativePorts {
    $existing = $null
    if (Test-Path -LiteralPath $script:PortsPath -PathType Leaf) {
        try { $existing = Get-Content -Raw -LiteralPath $script:PortsPath | ConvertFrom-Json } catch { $existing = $null }
    }
    if ($existing -and $existing.postgres -and $existing.redis -and $existing.api -and $existing.searxng) {
        $script:PostgresPort = [int]$existing.postgres
        $script:RedisPort = [int]$existing.redis
        $script:ApiPort = [int]$existing.api
        $script:SearxngPort = [int]$existing.searxng
        return
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
    $destination = Join-Path $script:Root ("deps\searxng\{0}" -f $artifact.version)
    $gitDirectory = Join-Path $destination ".git"
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container)) {
        if (Test-Path -LiteralPath $destination) {
            throw "SearXNG destination exists without a Git checkout: $destination"
        }
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        & git init $destination
        if ($LASTEXITCODE -ne 0) {
            throw "SearXNG Git repository initialization failed"
        }
        & git -C $destination remote add origin $artifact.url
        if ($LASTEXITCODE -ne 0) {
            throw "SearXNG Git remote configuration failed"
        }
    }
    if (-not $artifact.commit) {
        throw "SearXNG artifact must define a locked commit"
    }
    & git -C $destination fetch --depth 1 origin $artifact.commit
    if ($LASTEXITCODE -ne 0) {
        throw "SearXNG locked commit fetch failed"
    }
    $commit = (& git -C $destination rev-parse ("{0}^{{commit}}" -f $artifact.commit)).Trim()
    if ($artifact.commit -and -not $commit.Equals($artifact.commit, [StringComparison]::OrdinalIgnoreCase)) {
        throw "SearXNG commit verification failed: expected $($artifact.commit), got $commit"
    }
    if ($artifact.commit_prefix -and -not $commit.StartsWith($artifact.commit_prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "SearXNG commit prefix verification failed: expected $($artifact.commit_prefix), got $commit"
    }
    & git -C $destination update-ref refs/heads/native-locked $commit
    if ($LASTEXITCODE -ne 0) {
        throw "SearXNG locked ref creation failed"
    }
    & git -C $destination symbolic-ref HEAD refs/heads/native-locked
    if ($LASTEXITCODE -ne 0) {
        throw "SearXNG locked HEAD configuration failed"
    }
    $archiveUrl = if ($artifact.archive_url) {
        $artifact.archive_url
    } else {
        $repositoryUrl = $artifact.url -replace "\.git$", ""
        "$repositoryUrl/archive/$commit.zip"
    }
    $archivePath = Join-Path $script:Root ("downloads\searxng-{0}.zip" -f $artifact.version)
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        Invoke-WebRequest -UseBasicParsing -Uri $archiveUrl -OutFile $archivePath
    }
    if (Test-Path -LiteralPath $destination -PathType Container) {
        Get-ChildItem -LiteralPath $destination -Force |
            Where-Object { $_.Name -ne ".git" } |
            Remove-Item -Recurse -Force
    } else {
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    $destinationRoot = ([IO.Path]::GetFullPath($destination)).TrimEnd("\") + "\"
    try {
        foreach ($entry in $zip.Entries) {
            $parts = $entry.FullName -split "/", 2
            if ($parts.Count -lt 2) { continue }
            $relative = $parts[1]
            if ([string]::IsNullOrWhiteSpace($relative) -or $relative -eq "utils" -or $relative.StartsWith("utils/")) {
                continue
            }
            $target = [IO.Path]::GetFullPath((Join-Path $destination $relative.Replace("/", "\")))
            if (-not $target.StartsWith($destinationRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "SearXNG archive path escapes destination: $relative"
            }
            if ($entry.FullName.EndsWith("/")) {
                New-Item -ItemType Directory -Force -Path $target | Out-Null
            } else {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
            }
        }
    } finally {
        $zip.Dispose()
    }
    if (-not (Test-Path -LiteralPath (Join-Path $destination "setup.py") -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $destination "searx\__init__.py") -PathType Leaf)) {
        throw "SearXNG runtime source extraction is incomplete"
    }
    @'
import getpass
import os


class _PasswdEntry:
    def __init__(self, name, uid):
        self.pw_name = name
        self.pw_uid = uid


def getpwuid(uid):
    return _PasswdEntry(getpass.getuser() or os.environ.get("USERNAME", "unknown"), uid)
'@ | Set-Content -LiteralPath (Join-Path $destination "pwd.py") -Encoding ASCII
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
    [Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    return ([BitConverter]::ToString($buffer) -replace "-", "").ToLowerInvariant()
}

function ConvertTo-FernetKey {
    $buffer = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    return [Convert]::ToBase64String($buffer).Replace("+", "-").Replace("/", "_")
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
        "A$(New-RandomHex 24)!a1"
    }
    if ([string]::IsNullOrWhiteSpace($password)) {
        throw "native service account password is empty"
    }
    & net.exe user $accountName 2>$null | Out-Null
    $accountExists = $LASTEXITCODE -eq 0
    if ($accountExists) {
        & net.exe user $accountName $password "/active:yes" "/passwordchg:no" "/expires:never" | Out-Null
    } else {
        & net.exe user $accountName $password "/add" "/active:yes" "/passwordchg:no" "/expires:never" | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        throw "could not create or update the native service account; run install from an elevated PowerShell"
    }
    Set-Content -LiteralPath $passwordPath -Value $password -Encoding UTF8
    $principal = "{0}\{1}" -f $env:COMPUTERNAME, $accountName
    $grant = "{0}:(OI)(CI)M" -f $principal
    & icacls.exe $script:Root /grant:r $grant /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "could not grant the native service account access to the runtime directory"
    }
    if ($pythonRoot -and (Test-Path -LiteralPath $pythonRoot -PathType Container)) {
        $pythonGrant = "{0}:(OI)(CI)RX" -f $principal
        & icacls.exe $pythonRoot /grant:r $pythonGrant /C | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "could not grant the native service account access to the host Python runtime"
        }
    }
    return $principal
}

function Read-NativeServiceCredential {
    $passwordPath = Join-Path $script:Root "config\service-password.txt"
    if (-not (Test-Path -LiteralPath $passwordPath -PathType Leaf)) {
        throw "native service account is missing; run install first"
    }
    $password = (Get-Content -LiteralPath $passwordPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($password)) {
        throw "native service account password is empty"
    }
    $principal = "{0}\{1}" -f $env:COMPUTERNAME, (Get-NativeServiceAccountName)
    $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
    return [PSCredential]::new($principal, $securePassword)
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
    Set-Content -LiteralPath $script:EnvPath -Value $envText.Trim() -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $script:Root "config\postgres-password.txt") -Value $postgresPassword -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $script:Root "config\redis-password.txt") -Value $redisPassword -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $script:Root "config\admin-credentials.txt") -Value ("username=$AdminUsername`npassword=$adminPassword") -Encoding UTF8
}

function Write-NativeSearxSettings([string]$redisPassword) {
    $settingsPath = Join-Path $script:Root "config\searxng-settings.yml"
    @"
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
"@ | Set-Content -LiteralPath $settingsPath -Encoding UTF8
}

function Invoke-NativePython([string[]]$arguments) {
    $python = Join-Path $script:Root "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "native Python environment is missing; run install first"
    }
    Push-Location $script:Root
    try {
        & $python @arguments
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
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
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

function Get-WorkingSetReport($services) {
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $rows = foreach ($service in $services) {
        $processId = [int]$service.pid
        $ids = if ($processId -gt 0) { Get-ProcessTreeIds $processId $all } else { @() }
        $bytes = [int64]0
        foreach ($id in $ids) {
            if ($seen.Add($id)) {
                try { $bytes += [int64](Get-Process -Id $id -ErrorAction Stop).WorkingSet64 } catch { }
            }
        }
        [pscustomobject]@{
            service = $service.name
            role = $service.role
            pid = $processId
            healthy = ($processId -gt 0 -and [bool](Get-Process -Id $processId -ErrorAction SilentlyContinue))
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

function Start-ManagedProcess([string]$name, [string]$filePath, [string[]]$arguments, [string]$workingDirectory, [hashtable]$environment = @{}, [string]$role = $name, [string]$embeddedIn = $null, [PSCredential]$credential = $null) {
    $old = @{}
    foreach ($key in $environment.Keys) {
        $old[$key] = (Get-Item -Path ("Env:{0}" -f $key) -ErrorAction SilentlyContinue).Value
        Set-Item -Path ("Env:{0}" -f $key) -Value ([string]$environment[$key])
    }
    try {
        $logBase = Join-Path $script:Root ("logs\{0}" -f $name)
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
        }
        if ($environment.Count -gt 0) {
            $startOptions.Environment = $environment
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

function Test-ManagedProcess($service) {
    return $service.pid -and (Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue)
}

function Wait-PostgresReady([string]$pgIsReady) {
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        & $pgIsReady -h 127.0.0.1 -p $script:PostgresPort -U ashare 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "PostgreSQL did not become ready on port $script:PostgresPort"
}

function Wait-RedisReady([string]$redisCli, [string]$password) {
    $oldRedisAuth = $env:REDISCLI_AUTH
    try {
        $env:REDISCLI_AUTH = $password
        for ($attempt = 1; $attempt -le 30; $attempt++) {
            $reply = & $redisCli -h 127.0.0.1 -p $script:RedisPort ping 2>$null
            if ($LASTEXITCODE -eq 0 -and "$reply".Trim() -eq "PONG") {
                return
            }
            Start-Sleep -Seconds 1
        }
    } finally {
        if ($null -eq $oldRedisAuth) { Remove-Item Env:REDISCLI_AUTH -ErrorAction SilentlyContinue }
        else { $env:REDISCLI_AUTH = $oldRedisAuth }
    }
    throw "Redis did not become ready on port $script:RedisPort"
}

function Invoke-Install {
    Assert-ExternalRoot
    Initialize-Directories
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
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw "Python 3.11 or 3.12 is required on PATH for native installation" }
    $pythonVersion = (& $pythonCommand.Source -c "import sys; print('.'.join(map(str, sys.version_info[:2])))").Trim()
    if ($pythonVersion -notin @("3.11", "3.12")) { throw "Python 3.11 or 3.12 is required, found $pythonVersion" }
    $pythonwPath = Join-Path (Split-Path -Parent $pythonCommand.Source) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
        throw "pythonw.exe is missing from the host Python runtime"
    }
    $venvPython = Join-Path $script:Root "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        & $pythonCommand.Source -m venv (Join-Path $script:Root "venv")
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
    $postgresPassword = if (Test-Path (Join-Path $script:Root "config\postgres-password.txt")) { (Get-Content (Join-Path $script:Root "config\postgres-password.txt") -Raw).Trim() } else { New-RandomHex }
    $redisPassword = if (Test-Path (Join-Path $script:Root "config\redis-password.txt")) { (Get-Content (Join-Path $script:Root "config\redis-password.txt") -Raw).Trim() } else { New-RandomHex }
    $adminPasswordValue = if ($AdminPassword) { $AdminPassword } elseif (Test-Path (Join-Path $script:Root "config\admin-credentials.txt")) { ((Get-Content (Join-Path $script:Root "config\admin-credentials.txt") | Where-Object { $_ -like "password=*" }) -replace "^password=", "") } else { New-RandomHex }
    $fernetKey = if (Test-Path $script:EnvPath) { ((Get-Content $script:EnvPath | Where-Object { $_ -like "MODEL_SETTINGS_ENCRYPTION_KEYS=*" }) -replace "^MODEL_SETTINGS_ENCRYPTION_KEYS=", "") } else { ConvertTo-FernetKey }
    Write-NativeEnv $postgresPassword $redisPassword $adminPasswordValue $fernetKey
    $pythonSitePackages = Join-Path $script:Root "venv\Lib\site-packages"
    Set-Content -LiteralPath (Join-Path $script:Root "config\native-paths.json") -Value ([ordered]@{
        postgres_bin = $postgresBin
        redis_bin = $redisBin
        searxng_root = $searxngRoot
        python_exe = $pythonCommand.Source
        pythonw_exe = $pythonwPath
        python_site_packages = $pythonSitePackages
        postgres_port = $script:PostgresPort
        redis_port = $script:RedisPort
        api_port = $script:ApiPort
        searxng_port = $script:SearxngPort
        version = $script:NativeVersion
    } | ConvertTo-Json) -Encoding UTF8
    Set-Content -LiteralPath $script:PortsPath -Value ([ordered]@{
        postgres = $script:PostgresPort
        redis = $script:RedisPort
        api = $script:ApiPort
        searxng = $script:SearxngPort
    } | ConvertTo-Json) -Encoding UTF8
    $pythonRoot = Split-Path -Parent $pythonCommand.Source
    $serviceAccount = Ensure-NativeServiceAccount $pythonRoot
    $secretFile = Join-Path $script:Root "config\admin-credentials.txt"
    Write-Host "Native installation is ready at $script:Root"
    Write-Host "The generated administrator credentials are stored in $secretFile"
    Write-Host "Native services run as $serviceAccount"
}

function Invoke-Start {
    Assert-ExternalRoot
    if (-not (Test-Path -LiteralPath $script:EnvPath -PathType Leaf)) { throw "native .env is missing; run install first" }
    $pathsFile = Join-Path $script:Root "config\native-paths.json"
    if (-not (Test-Path -LiteralPath $pathsFile -PathType Leaf)) { throw "native paths are missing; run install first" }
    $paths = Get-Content -Raw -LiteralPath $pathsFile | ConvertFrom-Json
    if (-not $paths.postgres_port -or -not $paths.redis_port -or -not $paths.api_port -or -not $paths.searxng_port) {
        throw "native port configuration is missing; run install first"
    }
    $script:PostgresPort = [int]$paths.postgres_port
    $script:RedisPort = [int]$paths.redis_port
    $script:ApiPort = [int]$paths.api_port
    $script:SearxngPort = [int]$paths.searxng_port
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
    $serviceCredential = Read-NativeServiceCredential
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
            & $initdb -D $pgData -U ashare --pwfile=$pgPasswordFile --encoding=UTF8 --locale=C
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
        $services += Start-ManagedProcess "postgres" $postgres $postgresArguments $script:Root @{} "postgres" $null $serviceCredential
        Wait-PostgresReady $pgIsReady
        $createdb = Join-Path $paths.postgres_bin "createdb.exe"
        $psql = Join-Path $paths.postgres_bin "psql.exe"
        $oldPgPassword = $env:PGPASSWORD
        try {
            $env:PGPASSWORD = (Get-Content (Join-Path $script:Root "config\postgres-password.txt") -Raw).Trim()
            & $createdb -h 127.0.0.1 -p $script:PostgresPort -U ashare ashare 2>$null
            $createdbExit = $LASTEXITCODE
            if ($createdbExit -ne 0) {
                & $psql -h 127.0.0.1 -p $script:PostgresPort -U ashare -d ashare -c "SELECT 1" 2>$null | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "PostgreSQL database creation failed" }
            }
        } finally {
            if ($null -eq $oldPgPassword) { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
            else { $env:PGPASSWORD = $oldPgPassword }
        }
        New-Item -ItemType File -Force -Path (Join-Path $script:Root "state\database-ready") | Out-Null
        $redisConfig = Join-Path $script:Root "config\redis.conf"
        $redisPassword = (Get-Content (Join-Path $script:Root "config\redis-password.txt") -Raw).Trim()
        $redisCli = Join-Path $paths.redis_bin "redis-cli.exe"
        @("bind 127.0.0.1", "port $script:RedisPort", "protected-mode yes", "requirepass $redisPassword", "dir `"$($script:Root.Replace('\', '/'))/data/redis`"", "appendonly yes", "appendfsync everysec") | Set-Content -LiteralPath $redisConfig -Encoding UTF8
        $services += Start-ManagedProcess "redis" (Join-Path $paths.redis_bin "redis-server.exe") @($redisConfig) $script:Root @{} "redis" $null $serviceCredential
        Wait-RedisReady $redisCli $redisPassword
        Invoke-NativePython @("-m", "ashare_ai.cli", "migrate")
        Write-NativeSearxSettings $redisPassword
        $searxEnv = @{}
        foreach ($key in $envValues.Keys) { $searxEnv[$key] = $envValues[$key] }
        $searxEnv.SEARXNG_SETTINGS_PATH = (Join-Path $script:Root "config\searxng-settings.yml")
        $searxEnv.SEARXNG_BASE_URL = "http://127.0.0.1:$script:SearxngPort/"
        $searxEnv.GIT_CONFIG_GLOBAL = (Join-Path $script:Root "config\gitconfig")
        $searxPython = $pythonWindowlessExecutable
        $services += Start-ManagedProcess "searxng" $searxPython @("-m", "searx.webapp") $paths.searxng_root $searxEnv "searxng" $null $serviceCredential
        $apiEnv = @{}
        foreach ($key in $envValues.Keys) { $apiEnv[$key] = $envValues[$key] }
        $apiEnv.ASHARE_NATIVE_WEB_ROOT = (Join-Path $script:Root "web")
        $services += Start-ManagedProcess "api" $searxPython @("-m", "ashare_ai.cli", "api", "--host", "127.0.0.1", "--port", "$script:ApiPort") $script:Root $apiEnv "api" $null $serviceCredential
        $services += [pscustomobject]@{ name = "web"; role = "web"; pid = $services[-1].pid; embedded_in = "api"; started_at = [DateTime]::UtcNow.ToString("o") }
        $services += Start-ManagedProcess "job-worker" $searxPython @("-m", "ashare_ai.orchestration.serial_worker") $script:Root $envValues "job-worker" $null $serviceCredential
        $services += Start-ManagedProcess "exit-advice-worker" $searxPython @("-m", "ashare_ai.orchestration.exit_advice_worker") $script:Root $envValues "exit-advice-worker" $null $serviceCredential
        if ($ResearchMode -eq "DUAL" -and $ResearchWorkers -gt 0) {
            for ($index = 1; $index -le $ResearchWorkers; $index++) {
                $services += Start-ManagedProcess ("research-worker-{0}" -f $index) $searxPython @("-m", "ashare_ai.orchestration.research_worker") $script:Root $envValues "research-worker" $null $serviceCredential
            }
        }
        Write-State $services
    } catch {
        foreach ($service in @($services | Sort-Object @{ Expression = { if ($_.role -eq "postgres") { 0 } else { 1 } } })) {
            if ($service.embedded_in) { continue }
            if ($service.role -eq "postgres") {
                $pgCtl = Join-Path $paths.postgres_bin "pg_ctl.exe"
                $pgData = Join-Path $script:Root "data\postgres"
                & $pgCtl -D $pgData -m immediate stop 2>$null | Out-Null
            }
            $process = Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue
            if ($process) { Stop-Process -Id $process.Id -Force }
        }
        Write-State @()
        throw
    } finally { Pop-Location }
    Write-Host "Native API/Web: http://127.0.0.1:$script:ApiPort/"
}

function Invoke-Stop {
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
            & $pgCtl -D $pgData -m fast stop 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host ("Stopped {0} (graceful)" -f $service.name)
                continue
            }
        }
        $process = Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -Id $process.Id -Force
            Write-Host ("Stopped {0} (PID {1})" -f $service.name, $service.pid)
        }
    }
    Write-State @()
}

function Invoke-Status {
    $services = @(Read-State)
    $report = Get-WorkingSetReport $services
    if ($Json) { $report | ConvertTo-Json -Depth 8; return }
    if ($services.Count -eq 0) { Write-Host "Native runtime is stopped"; return }
    $report.services | Format-Table service, role, pid, healthy, working_set_mib, embedded_in -AutoSize
    Write-Host ("Process-group working set: {0} MiB" -f $report.total_working_set_mib)
}

function Invoke-Doctor {
    Assert-ExternalRoot
    $checks = @()
    $checks += [pscustomobject]@{ check = "runtime-root-outside-source"; status = if ($script:Root -notlike "$script:SourceRoot*") { "PASS" } else { "FAIL" }; detail = $script:Root }
    $checks += [pscustomobject]@{ check = "native-env"; status = if (Test-Path $script:EnvPath) { "PASS" } else { "FAIL" }; detail = $script:EnvPath }
    $checks += [pscustomobject]@{ check = "python"; status = if (Test-Path (Join-Path $script:Root "venv\Scripts\python.exe")) { "PASS" } else { "FAIL" }; detail = "venv" }
    $checks += [pscustomobject]@{ check = "web-index"; status = if (Test-Path (Join-Path $script:Root "web\index.html")) { "PASS" } else { "FAIL" }; detail = "static SPA" }
    $checks += [pscustomobject]@{ check = "docker-wsl-processes"; status = if (@(Get-Process -Name "docker*","wsl*" -ErrorAction SilentlyContinue).Count -eq 0) { "PASS" } else { "WARN" }; detail = "native entry does not start Docker or WSL" }
    if ($Json) { $checks | ConvertTo-Json -Depth 5; return }
    $checks | Format-Table check, status, detail -AutoSize
    if (@($checks | Where-Object status -eq "FAIL").Count -gt 0) { exit 1 }
}

switch ($Command) {
    "install" { Invoke-Install }
    "start" { Invoke-Start }
    "stop" { Invoke-Stop }
    "status" { Invoke-Status }
    "doctor" { Invoke-Doctor }
}
