# Usage: transfer_start.bat [start|stop|restart|status]
# Runs the transfer node in the background (survives closing the window / SSH session).
# Python: -Python, else $env:TRANSFER_PYTHON, else .venv, else conda env "transfer_node_3Dr".
param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'start',
    [int]$Port = 13001,
    [string]$Python = $env:TRANSFER_PYTHON,
    [string]$RepoRoot = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path $RepoRoot).Path
$LogDir = Join-Path $RepoRoot 'logs'
$Log = Join-Path $LogDir 'transfer.log'

function Find-Python {
    $candidates = @(
        $Python,
        (Join-Path $RepoRoot '.venv\Scripts\python.exe'),
        (Join-Path $env:USERPROFILE '.conda\envs\transfer_node_3Dr\python.exe')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path }
    }
    throw "找不到 Python，请用 -Python 或环境变量 TRANSFER_PYTHON 指定"
}

function Get-TransferProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match 'transfer_node\.main:app' -and $_.CommandLine -match "--port\s+$Port\b" }
}

# Tunnel ssh processes spawned by the transfer node, including ones orphaned by a crash.
function Get-TunnelProcesses([int[]]$ParentIds) {
    $alive = @(Get-Process | ForEach-Object Id)
    Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" |
        Where-Object {
            $_.CommandLine -match '-N -o BatchMode=yes -o ExitOnForwardFailure=yes' -and
            (($ParentIds -contains $_.ParentProcessId) -or ($alive -notcontains $_.ParentProcessId))
        }
}

function Test-Health {
    try {
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$Port/health")
        $req.Proxy = $null
        $req.Timeout = 3000
        $resp = $req.GetResponse()
        try { return (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd() }
        finally { $resp.Close() }
    } catch {
        return $null
    }
}

function Stop-Transfer {
    $procs = @(Get-TransferProcesses)
    $ids = @($procs | ForEach-Object ProcessId)
    $parents = @($procs | ForEach-Object ParentProcessId)
    $tunnels = @(Get-TunnelProcesses $ids)
    if (-not $procs -and -not $tunnels) {
        Write-Host "中转节点未在运行"
        return
    }
    foreach ($id in $ids + @($tunnels | ForEach-Object ProcessId)) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" |
        Where-Object { $parents -contains $_.ProcessId -and $_.CommandLine -match 'transfer_node\.main:app' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Write-Host "中转节点已停止 (结束 $($ids.Count) 个服务进程, $($tunnels.Count) 个隧道进程)"
}

function Start-Transfer {
    $running = @(Get-TransferProcesses)
    if ($running) {
        Write-Host "中转节点已在运行 (pid $($running[0].ProcessId))"
        return
    }
    if (Test-Health) { throw "端口 $Port 已被其他程序占用" }
    $orphans = @(Get-TunnelProcesses @())
    foreach ($o in $orphans) { Stop-Process -Id $o.ProcessId -Force -ErrorAction SilentlyContinue }

    $py = Find-Python
    $envDir = Split-Path -Parent $py
    if ((Split-Path -Leaf $envDir) -eq 'Scripts') { $envDir = Split-Path -Parent $envDir }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 50MB) { Move-Item -Force $Log "$Log.old" }
    Add-Content -Encoding UTF8 -Path $Log -Value "===== $(Get-Date -Format s) start transfer node: $py ====="

    # Win32_Process.Create detaches the process from this console / SSH session.
    $cmd = "cmd.exe /c `"cd /d `"$RepoRoot`" && set PYTHONUNBUFFERED=1 && set PYTHONIOENCODING=utf-8 && " +
           "set `"PATH=$envDir;$envDir\Library\bin;$envDir\Scripts;%PATH%`" && " +
           "`"$py`" -m uvicorn transfer_node.main:app --host 0.0.0.0 --port $Port --log-level info >> `"$Log`" 2>&1`""
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
    if ($r.ReturnValue -ne 0) { throw "进程创建失败 (Win32_Process.Create 返回 $($r.ReturnValue))" }

    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $health = Test-Health
        if ($health) {
            Write-Host "中转节点已启动 (pid $((Get-TransferProcesses | Select-Object -First 1).ProcessId))，日志: $Log"
            Write-Host $health
            return
        }
        if ($i -ge 5 -and -not (Get-TransferProcesses)) { break }
    }
    Write-Host "启动失败，日志最后 20 行:" -ForegroundColor Red
    Get-Content $Log -Tail 20
    exit 1
}

function Show-Status {
    $procs = @(Get-TransferProcesses)
    if ($procs) {
        Write-Host "中转节点运行中 (pid $($procs[0].ProcessId), 启动于 $($procs[0].CreationDate))"
        $health = Test-Health
        if ($health) { Write-Host $health } else { Write-Host "/health 无响应" }
    } else {
        Write-Host "中转节点未在运行"
    }
    $tunnels = @(Get-TunnelProcesses @($procs | ForEach-Object ProcessId))
    foreach ($t in $tunnels) { Write-Host ("隧道 pid {0}: {1}" -f $t.ProcessId, ($t.CommandLine -split ' ')[-1]) }
}

switch ($Action) {
    'start' { Start-Transfer }
    'stop' { Stop-Transfer }
    'restart' { Stop-Transfer; Start-Transfer }
    'status' { Show-Status }
}
