[CmdletBinding()]
param([string]$Root, [string]$SourceRoot)
& (Join-Path $PSScriptRoot "ashare-native.ps1") -Command start -Root $Root -SourceRoot $SourceRoot
exit $LASTEXITCODE
