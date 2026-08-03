[CmdletBinding()]
param(
    [switch]$SkipPackage,
    [switch]$SkipWebBuild
)

$ErrorActionPreference = "Stop"
$compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw ".NET Framework 4.x C# compiler was not found: $compiler" }
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$output = Join-Path $PSScriptRoot "AshareAI.NativeControlCenter.exe"
$cliOutput = Join-Path $PSScriptRoot "AshareAI.NativeControlCenter.Cli.exe"
$manifest = Join-Path $PSScriptRoot "app.manifest"
$managerSources = @(
    (Join-Path $PSScriptRoot "Program.cs"),
    (Join-Path $PSScriptRoot "CommandSupport.cs")
)
& $compiler /nologo /target:winexe /optimize+ /platform:anycpu /utf8output `
    /main:AshareAI.NativeControlCenter.Program `
    "/win32manifest:$manifest" `
    /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll `
    /reference:System.Web.Extensions.dll /reference:System.Windows.Forms.dll `
    "/resource:$(Join-Path $sourceRoot 'scripts\native\ashare-native.ps1'),AshareAI.Controller" `
    "/resource:$(Join-Path $sourceRoot 'scripts\native\dependencies.lock.json'),AshareAI.DependencyLock" `
    /out:$output $managerSources
if ($LASTEXITCODE -ne 0) { throw "Native Control Center compilation failed with exit code $LASTEXITCODE" }
& icacls.exe $output /grant:r "Everyone:(RX)" /grant "BUILTIN\Administrators:(F)" | Out-Null
Write-Host "Built $output"

$cliSources = @(
    (Join-Path $PSScriptRoot "Cli.cs"),
    (Join-Path $PSScriptRoot "Program.cs"),
    (Join-Path $PSScriptRoot "CommandSupport.cs")
)
& $compiler /nologo /target:exe /optimize+ /platform:anycpu /utf8output `
    /main:AshareAI.NativeControlCenter.CliProgram `
    /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll `
    /reference:System.Web.Extensions.dll /reference:System.Windows.Forms.dll `
    "/resource:$(Join-Path $sourceRoot 'scripts\native\ashare-native.ps1'),AshareAI.Controller" `
    "/resource:$(Join-Path $sourceRoot 'scripts\native\dependencies.lock.json'),AshareAI.DependencyLock" `
    /out:$cliOutput $cliSources
if ($LASTEXITCODE -ne 0) { throw "Native Control Center CLI compilation failed with exit code $LASTEXITCODE" }
& icacls.exe $cliOutput /grant:r "Everyone:(RX)" /grant "BUILTIN\Administrators:(F)" | Out-Null
Write-Host "Built $cliOutput"
if ($SkipPackage) { return }

$dist = Join-Path $PSScriptRoot "dist"
$stage = Join-Path $dist "stage"
$payload = Join-Path $dist "payload.zip"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
if (Test-Path -LiteralPath $stage) { Remove-Item -Recurse -Force -LiteralPath $stage }
New-Item -ItemType Directory -Force -Path (Join-Path $stage "app") | Out-Null
Copy-Item -Force $output (Join-Path $stage "AshareAI.NativeControlCenter.exe")
Copy-Item -Force $cliOutput (Join-Path $stage "AshareAI.NativeControlCenter.Cli.exe")
Copy-Item -Force (Join-Path $PSScriptRoot "ashareai.cmd") (Join-Path $stage "ashareai.cmd")
foreach ($directory in @("src", "configs", "migrations")) { Copy-Item -Recurse -Force (Join-Path $sourceRoot $directory) (Join-Path $stage "app\$directory") }
foreach ($file in @("pyproject.toml", "requirements.runtime.lock", "alembic.ini", "README.md", "LICENSE")) { Copy-Item -Force (Join-Path $sourceRoot $file) (Join-Path $stage "app\$file") }

$webRoot = Join-Path $sourceRoot "web"
if (-not $SkipWebBuild) {
    Push-Location $webRoot
    try {
        & npm.cmd ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "web build failed with exit code $LASTEXITCODE" }
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath (Join-Path $webRoot "dist\index.html") -PathType Leaf)) { throw "web/dist is missing" }
New-Item -ItemType Directory -Force -Path (Join-Path $stage "app\web") | Out-Null
Copy-Item -Recurse -Force (Join-Path $webRoot "dist") (Join-Path $stage "app\web\dist")

$vendor = Join-Path $stage "app\vendor"
New-Item -ItemType Directory -Force -Path $vendor | Out-Null
$pythonInstaller = Join-Path $vendor "python-3.12.10-amd64.exe"
Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $pythonInstaller
if ((Get-FileHash -Algorithm SHA256 $pythonInstaller).Hash.ToLowerInvariant() -ne "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb") { throw "bundled Python checksum mismatch" }
$lock = Get-Content -Raw (Join-Path $sourceRoot "scripts\native\dependencies.lock.json") | ConvertFrom-Json
$searxng = $lock.artifacts | Where-Object id -eq "searxng"
$searxngArchive = Join-Path $vendor "searxng.zip"
Invoke-WebRequest -UseBasicParsing -Uri $searxng.archive_url -OutFile $searxngArchive
if ((Get-FileHash -Algorithm SHA256 $searxngArchive).Hash.ToLowerInvariant() -ne ([string]$searxng.sha256).ToLowerInvariant()) { throw "bundled SearXNG checksum mismatch" }

if (Test-Path -LiteralPath $payload) { Remove-Item -Force -LiteralPath $payload }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $payload -CompressionLevel Optimal
$setup = Join-Path $dist "AshareAI-Setup.exe"
& $compiler /nologo /target:winexe /optimize+ /platform:anycpu /utf8output `
    "/win32manifest:$(Join-Path $PSScriptRoot 'setup.manifest')" `
    /reference:System.dll /reference:System.Core.dll /reference:System.IO.Compression.dll `
    /reference:System.IO.Compression.FileSystem.dll /reference:System.Windows.Forms.dll `
    "/resource:$payload,AshareAI.Payload" /out:$setup (Join-Path $PSScriptRoot "Installer.cs")
if ($LASTEXITCODE -ne 0) { throw "setup compilation failed with exit code $LASTEXITCODE" }
Write-Host "Built installer $setup"
