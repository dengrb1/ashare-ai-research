@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ashare-native.ps1" %*
exit /b %ERRORLEVEL%
