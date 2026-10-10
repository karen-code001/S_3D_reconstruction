@echo off
rem Usage: transfer_start.bat [start^|stop^|restart^|status]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0transfer_start.ps1" %*
if "%~1"=="" pause
