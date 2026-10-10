# Keeps one SSH tunnel to a GPU server alive; started by a "3DR-Tunnel-<name>" scheduled task.
#
# -LocalForwards "14001:14000,14002:14010" means local 127.0.0.1:14001 -> GPU server 127.0.0.1:14000, ...
# -CallbackPort is the port opened on the GPU server that leads back to this transfer node.
param(
    [Parameter(Mandatory)] [string]$Name,
    [Parameter(Mandatory)] [string]$Target,
    [Parameter(Mandatory)] [int]$SshPort,
    [Parameter(Mandatory)] [string]$KeyFile,
    [Parameter(Mandatory)] [string]$LocalForwards,
    [Parameter(Mandatory)] [int]$CallbackPort,
    [int]$TransferPort = 13001
)

$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "tunnel-$Name.log"
$KnownHosts = Join-Path (Split-Path -Parent $KeyFile) 'known_hosts'
$Ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
if (-not (Test-Path $Ssh)) { $Ssh = (Get-Command ssh.exe -ErrorAction Stop).Source }

$sshArgs = @(
    '-N', '-T',
    '-p', "$SshPort",
    '-i', $KeyFile,
    '-o', 'BatchMode=yes',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', "UserKnownHostsFile=$KnownHosts"
)
foreach ($pair in $LocalForwards.Split(',', [StringSplitOptions]::RemoveEmptyEntries)) {
    $local, $remote = $pair.Trim().Split(':')
    $sshArgs += @('-L', "127.0.0.1:${local}:127.0.0.1:${remote}")
}
$sshArgs += @('-R', "127.0.0.1:${CallbackPort}:127.0.0.1:${TransferPort}", $Target)

while ($true) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 10MB) {
        Move-Item -Force $Log "$Log.old"
    }
    Add-Content -Encoding UTF8 -Path $Log -Value "===== $(Get-Date -Format s) ssh $($sshArgs -join ' ') ====="
    & $Ssh @sshArgs 2>&1 | ForEach-Object { "$_" } | Add-Content -Encoding UTF8 -Path $Log
    Add-Content -Encoding UTF8 -Path $Log -Value "===== $(Get-Date -Format s) ssh exited with code $LASTEXITCODE, reconnecting in 10s ====="
    Start-Sleep -Seconds 10
}
