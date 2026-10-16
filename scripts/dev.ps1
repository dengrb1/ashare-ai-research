[CmdletBinding()]
param(
    [ValidateSet("web", "native", "installer", "console", "check", "start", "all")]
    [string]$Task = "all",
    [switch]$SkipWebBuild,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$webRoot = Join-Path $repoRoot "web"
$nativeBuild = Join-Path $repoRoot "windows\native-control-center\build.ps1"
$consoleBuild = Join-Path $repoRoot "tools\console\build.ps1"

function Invoke-Step([string]$Name, [scriptblock]$Action) {
    Write-Host ("`n== {0} ==" -f $Name) -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "{0} failed with exit code {1}" -f $Name, $LASTEXITCODE }
}

function Build-Web {
    Push-Location $webRoot
    try {
        Invoke-Step "Web dependencies" { npm.cmd ci --ignore-scripts }
        Invoke-Step "Web build" { npm.cmd run build }
    } finally { Pop-Location }
}

function Build-Native([switch]$Package, [switch]$WebAlreadyBuilt) {
    $arguments = @{}
    if ($SkipWebBuild -or $WebAlreadyBuilt) { $arguments.SkipWebBuild = $true }
    if (-not $Package) { $arguments.SkipPackage = $true }
    Invoke-Step ($(if ($Package) { "Windows installer" } else { "Windows control center" })) { & $nativeBuild @arguments }
}

switch ($Task) {
    "web" { Build-Web }
    "native" { Build-Native }
    "installer" { Build-Native -Package }
    "console" { Invoke-Step "Developer console" { & $consoleBuild } }
    "check" {
        if (-not $SkipTests) {
            Invoke-Step "Python tests" { $env:PYTHONPATH = "src"; .\.venv\Scripts\python.exe -m pytest }
            Push-Location $webRoot
            try { Invoke-Step "Web tests" { npm.cmd test -- --run } } finally { Pop-Location }
        }
        Build-Web
        Invoke-Step "Git diff check" { git diff --check }
    }
    "start" { Invoke-Step "Native runtime" { & (Join-Path $repoRoot "scripts\native\ashare-native.ps1") -Command start -Root (Join-Path $repoRoot ".runtime") -SourceRoot $repoRoot } }
    "all" {
        if (-not $SkipWebBuild) { Build-Web }
        Build-Native -Package -WebAlreadyBuilt
        Invoke-Step "Developer console" { & $consoleBuild }
    }
}

Write-Host ("`n完成：{0}" -f $Task) -ForegroundColor Green
