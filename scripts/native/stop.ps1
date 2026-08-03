[CmdletBinding()]
param([string]$Root, [string]$SourceRoot)
& (Join-Path $PSScriptRoot "ashare-native.ps1") -Command stop -Root $Root -SourceRoot $SourceRoot
exit $LASTEXITCODE
