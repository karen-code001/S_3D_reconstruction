@echo off
rem Double-click to deploy/upgrade this PC as a transfer node. Extra args are passed to deploy_transfer.ps1,
rem e.g.  deploy_transfer.bat -CallbackPort 13002
net session >nul 2>&1
if errorlevel 1 (
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    ) else (
        powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0' -ArgumentList '%*'"
    )
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_transfer.ps1" %*
pause
