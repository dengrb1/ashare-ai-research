$ErrorActionPreference = "Stop"
$root = "F:\Progress\AshareAI\runtime"
$pw = (Get-Content -LiteralPath (Join-Path $root "config\service-password.txt") -Raw).Trim()
$principal = "$env:COMPUTERNAME\AshareAIService"
$secure = ConvertTo-SecureString $pw -AsPlainText -Force
$cred = [PSCredential]::new($principal, $secure)
$out = Join-Path $env:TEMP "watchdog-repro.out.log"
$err = Join-Path $env:TEMP "watchdog-repro.err.log"
Remove-Item $out, $err -Force -ErrorAction SilentlyContinue
$args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "F:\Progress\AshareAI\runtime\controller\ashare-native.ps1", "-Command", "watchdog", "-Root", "F:\Progress\AshareAI\runtime", "-SourceRoot", "F:\code\ashare-ai-src", "-WatchdogIntervalSeconds", "10")
$p = Start-Process -FilePath (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") -ArgumentList $args -Credential $cred -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -Wait
"exit=" + $p.ExitCode
"--- stderr ---"
if (Test-Path $err) { Get-Content $err }
"--- stdout ---"
if (Test-Path $out) { Get-Content $out | Select-Object -Last 15 }
