[CmdletBinding()]
param([string]$Root, [string]$SourceRoot, [ValidateSet("SERIAL", "DUAL")][string]$ResearchMode = "SERIAL", [ValidateRange(0, 2)][int]$ResearchWorkers = 0)
& (Join-Path $PSScriptRoot "ashare-native.ps1") -Command start -Root $Root -SourceRoot $SourceRoot -ResearchMode $ResearchMode -ResearchWorkers $ResearchWorkers
exit $LASTEXITCODE
