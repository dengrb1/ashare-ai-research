[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "service_control.ps1") -Action Stop -Services Stack
