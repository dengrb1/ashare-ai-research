[CmdletBinding()]
param([string]$Root, [string]$SourceRoot, [string]$AdminUsername = "admin", [string]$AdminPassword)
& (Join-Path $PSScriptRoot "ashare-native.ps1") -Command install -Root $Root -SourceRoot $SourceRoot -AdminUsername $AdminUsername -AdminPassword $AdminPassword
exit $LASTEXITCODE
