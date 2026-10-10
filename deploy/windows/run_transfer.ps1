# Keeps the transfer node running; started by the "3DR-Transfer" scheduled task.
param(
    [int]$Port = 13001
)

$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'transfer.log'

# pydantic-settings reads .env from the working directory, and `common` must be importable.
Set-Location $RepoRoot
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

while ($true) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 50MB) {
        Move-Item -Force $Log "$Log.old"
    }
    Add-Content -Encoding UTF8 -Path $Log -Value "===== $(Get-Date -Format s) starting transfer node on port $Port ====="
    & $Python -m uvicorn transfer_node.main:app --host 0.0.0.0 --port $Port --log-level info 2>&1 |
        ForEach-Object { "$_" } | Add-Content -Encoding UTF8 -Path $Log
    Add-Content -Encoding UTF8 -Path $Log -Value "===== $(Get-Date -Format s) transfer node exited with code $LASTEXITCODE, restarting in 5s ====="
    Start-Sleep -Seconds 5
}
