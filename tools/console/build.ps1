# Build the one-click console exe with PyInstaller.
# Usage: pwsh -File tools\console\build.ps1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$consoleDir = Join-Path $repoRoot "tools\console"
$distDir = Join-Path $consoleDir "dist"
$buildDir = Join-Path $consoleDir "build"
$outExe = Join-Path $distDir "A-Share-AI-Trader-Console.exe"
$rootExe = Join-Path $repoRoot "A-Share-AI-Trader-Console.exe"

python -m pip install --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pyinstaller install failed" }

python -m PyInstaller --noconfirm --clean `
    --onefile --windowed `
    --name "A-Share-AI-Trader-Console" `
    --distpath $distDir `
    --workpath $buildDir `
    --specpath $consoleDir `
    (Join-Path $consoleDir "console_app.py")
if ($LASTEXITCODE -ne 0) { throw "pyinstaller build failed" }

if (-not (Test-Path $outExe)) { throw "expected output missing: $outExe" }
Write-Host "Built: $outExe" -ForegroundColor Green
try {
    Copy-Item -LiteralPath $outExe -Destination $rootExe -Force -ErrorAction Stop
    Write-Host "Published: $rootExe" -ForegroundColor Green
} catch {
    # A running console keeps its own exe locked on Windows. Never terminate a
    # user's console just to publish; leave a deterministic handoff file and
    # let the next build (after closing the window) publish normally.
    $pendingExe = Join-Path $repoRoot "A-Share-AI-Trader-Console.pending.exe"
    Copy-Item -LiteralPath $outExe -Destination $pendingExe -Force
    Write-Warning "无法覆盖正在运行的 exe: $rootExe"
    Write-Warning "请关闭当前控制台后重新运行 build.ps1；临时构建位于 $pendingExe"
}
