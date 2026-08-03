[CmdletBinding()]
param([string]$Root, [string]$SourceRoot, [switch]$Json)
& (Join-Path $PSScriptRoot "ashare-native.ps1") -Command status -Root $Root -SourceRoot $SourceRoot -Json:$Json
exit $LASTEXITCODE
