[CmdletBinding()]
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "service_control.ps1") -Action Start -Services Stack -SkipBuild:$SkipBuild
