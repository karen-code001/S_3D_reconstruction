<#
.SYNOPSIS
    把当前 Windows 电脑部署（或升级）为三维重建中转节点 transfer_node。

.DESCRIPTION
    读取同目录的 cluster.json，依次完成：
      安装 Python -> 创建虚拟环境并安装依赖 -> 生成 .env -> 防火墙放行 -> 关闭睡眠
      -> 配置 SSH 免密 -> 注册开机自启的计划任务（中转服务 + 每台 GPU 服务器一条隧道）-> 自检
    重复运行即为升级：会先停掉旧任务，再按最新 cluster.json 重新生成配置并启动。

.EXAMPLE
    .\deploy_transfer.ps1
    .\deploy_transfer.ps1 -CallbackPort 13002
    .\deploy_transfer.ps1 -NodeId transfer_lab_01 -PublicIp 10.76.135.221
    .\deploy_transfer.ps1 -Uninstall
#>
param(
    [string]$ClusterConfig = (Join-Path (Split-Path -Parent $PSScriptRoot) 'cluster.json'),
    [string]$NodeId,
    [int]$CallbackPort,
    [string]$PublicIp,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$TaskPrefix = '3DR-'
$TransferTaskName = "${TaskPrefix}Transfer"
$FirewallRuleName = '3DR transfer node'
$KeyFile = Join-Path $env:USERPROFILE '.ssh\id_ed25519_3dr'
[Net.WebRequest]::DefaultWebProxy = New-Object Net.WebProxy
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "    [OK] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    [!!] $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "    [XX] $text" -ForegroundColor Red }

function Invoke-Native {
    param([string]$FilePath, [string[]]$ArgumentList = @(), [switch]$Stream)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Stream) {
            & $FilePath @ArgumentList | Out-Host
            $output = ''
        } else {
            $output = (& $FilePath @ArgumentList 2>&1 | ForEach-Object { "$_" }) -join "`n"
        }
        $code = $LASTEXITCODE
    } catch {
        $output = "$_"
        $code = -1
    } finally {
        $ErrorActionPreference = $previous
    }
    [pscustomobject]@{ ExitCode = $code; Output = $output }
}

function Stop-3drTasks {
    Get-ScheduledTask -TaskName "$TaskPrefix*" -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue
    }
    # Child processes can outlive the task's PowerShell host.
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='ssh.exe'" | Where-Object {
        $_.CommandLine -like '*transfer_node.main:app*' -or $_.CommandLine -like "*$KeyFile*"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Find-Python {
    $versionCheck = "import sys; print('%d.%d %s' % (sys.version_info[0], sys.version_info[1], sys.executable))"
    $candidates = New-Object System.Collections.Generic.List[object]
    foreach ($v in '3.13', '3.12', '3.11', '3.10') { $candidates.Add(@('py', "-$v")) }
    foreach ($v in '313', '312', '311', '310') {
        $candidates.Add(@("$env:ProgramFiles\Python$v\python.exe"))
        $candidates.Add(@("$env:LOCALAPPDATA\Programs\Python\Python$v\python.exe"))
    }
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*WindowsApps*' }
    if ($onPath) { $candidates.Add(@($onPath.Source)) }

    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        if ($exe -ne 'py' -and -not (Test-Path $exe)) { continue }
        $extra = @($candidate | Select-Object -Skip 1)
        $result = Invoke-Native $exe ($extra + @('-c', $versionCheck))
        if ($result.ExitCode -ne 0 -or -not $result.Output) { continue }
        $version, $path = ($result.Output -split "`n")[-1].Trim().Split(' ', 2)
        if ([version]$version -ge [version]'3.10') { return $path }
    }
    return $null
}

function Install-Python($cfg) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host '    使用 winget 安装 Python...'
        Invoke-Native $winget.Source @('install', '-e', '--id', "Python.Python.$($cfg.python_version)",
            '--scope', 'machine', '--silent', '--accept-package-agreements', '--accept-source-agreements') -Stream | Out-Null
        $found = Find-Python
        if ($found) { return $found }
        Warn 'winget 安装后仍未找到 Python，改为下载安装包'
    }
    $installer = Join-Path $env:TEMP 'python-3dr-installer.exe'
    Write-Host "    下载 $($cfg.python_installer_url)"
    Invoke-WebRequest -UseBasicParsing -Uri $cfg.python_installer_url -OutFile $installer
    $proc = Start-Process -FilePath $installer -Wait -PassThru `
        -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0'
    if ($proc.ExitCode -ne 0) { throw "Python 安装程序退出码 $($proc.ExitCode)" }
    return Find-Python
}

function Get-OutboundIp([string]$url) {
    $uri = [Uri]$url
    $address = [Net.Dns]::GetHostAddresses($uri.Host) |
        Where-Object { $_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork } | Select-Object -First 1
    $socket = New-Object Net.Sockets.Socket([Net.Sockets.AddressFamily]::InterNetwork, [Net.Sockets.SocketType]::Dgram, [Net.Sockets.ProtocolType]::Udp)
    try {
        $socket.Connect($address, $uri.Port)
        return $socket.LocalEndPoint.Address.ToString()
    } finally {
        $socket.Close()
    }
}

function Test-TcpPort([string]$hostName, [int]$port, [int]$timeoutMs = 3000) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($hostName, $port, $null, $null)
        return ($async.AsyncWaitHandle.WaitOne($timeoutMs) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Write-Utf8NoBom([string]$path, [string]$text) {
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
}

function Register-3drTask([string]$name, [string]$script, [string]$arguments) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $RepoRoot `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" $arguments"
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # S4U: runs at boot without anyone logged in and without storing a password; keeps the user's SSH key.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $name
}

# ---------------------------------------------------------------------------

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Fail '需要管理员权限：请右键"以管理员身份运行"，或直接双击 deploy_transfer.bat'
    exit 1
}

if ($Uninstall) {
    Step '卸载中转节点的计划任务和防火墙规则（代码和数据保留）'
    Stop-3drTasks
    Get-ScheduledTask -TaskName "$TaskPrefix*" -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
    Remove-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
    Ok '已卸载'
    exit 0
}

Step "读取集群配置 $ClusterConfig"
$cfg = Get-Content -Raw -Encoding UTF8 $ClusterConfig | ConvertFrom-Json
$port = [int]$cfg.transfer_port

if (-not $NodeId) {
    $NodeId = 'transfer_' + ($env:COMPUTERNAME -replace '[^A-Za-z0-9_-]', '_')
}
$known = @{}
if ($cfg.transfer_nodes) {
    foreach ($p in $cfg.transfer_nodes.PSObject.Properties) { $known[$p.Name] = [int]$p.Value.callback_port }
}
if (-not $CallbackPort) {
    if ($known.ContainsKey($NodeId)) {
        $CallbackPort = $known[$NodeId]
    } else {
        $used = @($known.Values) + @($port)
        $CallbackPort = ($used | Measure-Object -Maximum).Maximum + 1
        Warn "cluster.json 的 transfer_nodes 里没有 $NodeId，自动分配回传端口 $CallbackPort"
        Warn "部署完成后请把 `"$NodeId`": { `"callback_port`": $CallbackPort } 加进共享的 cluster.json，避免别的电脑重复使用"
        $answer = Read-Host '    直接回车确认，或输入其他端口号'
        if ($answer.Trim()) { $CallbackPort = [int]$answer.Trim() }
    }
}
foreach ($other in $known.Keys) {
    if ($other -ne $NodeId -and $known[$other] -eq $CallbackPort) {
        throw "回传端口 $CallbackPort 已被 $other 使用，请用 -CallbackPort 指定其他端口"
    }
}
Ok "节点 ID: $NodeId，回传端口: $CallbackPort"

Step '停止旧的中转服务和隧道'
Stop-3drTasks
Ok '已停止'

Step '检查 Python（需要 3.10 及以上）'
$python = Find-Python
if (-not $python) { $python = Install-Python $cfg }
if (-not $python) { throw '没有找到可用的 Python 3.10+，请手动安装后重试' }
Ok $python

Step '创建虚拟环境并安装依赖'
$venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    $r = Invoke-Native $python @('-m', 'venv', (Join-Path $RepoRoot '.venv'))
    if ($r.ExitCode -ne 0) { throw "创建虚拟环境失败：$($r.Output)" }
}
$pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $RepoRoot 'requirements.txt'))
if ($cfg.pip_index_url) { $pipArgs += @('-i', $cfg.pip_index_url) }
$r = Invoke-Native $venvPython $pipArgs -Stream
if ($r.ExitCode -ne 0) { throw 'pip 安装依赖失败，见上方输出' }
Ok '依赖已安装'

Step '生成 .env'
if (-not $PublicIp) { $PublicIp = Get-OutboundIp $cfg.control_plane_url }
Ok "本机对外 IP: $PublicIp（不对的话用 -PublicIp 指定）"
$computeEntries = foreach ($h in $cfg.ssh_hosts) {
    foreach ($n in $h.compute_nodes) { "$($n.node_id)|http://127.0.0.1:$($n.local_port)|$($n.weight)" }
}
$envLines = @(
    "# Generated by deploy/windows/deploy_transfer.ps1 at $(Get-Date -Format s); edit deploy/cluster.json instead.",
    "TRANSFER_NODE_ID=$NodeId",
    "TRANSFER_PUBLIC_URL=http://${PublicIp}:$port",
    "TRANSFER_CALLBACK_URL=http://127.0.0.1:$CallbackPort",
    "TRANSFER_CONTROL_PLANE_URL=$($cfg.control_plane_url)",
    "TRANSFER_STORAGE_ROOT=$($cfg.storage_root)",
    "TRANSFER_UPLOAD_TOKEN_SECRET=$($cfg.upload_token_secret)",
    "TRANSFER_INTERNAL_API_KEY=$($cfg.internal_api_key)",
    "TRANSFER_HEARTBEAT_INTERVAL_SECONDS=$($cfg.heartbeat_interval_seconds)",
    "TRANSFER_DISPATCH_INTERVAL_SECONDS=$($cfg.dispatch_interval_seconds)",
    "TRANSFER_MAX_CONCURRENT_UPLOADS=$($cfg.max_concurrent_uploads)",
    "TRANSFER_CORS_ORIGINS=$($cfg.cors_origins -join ', ')",
    "TRANSFER_COMPUTE_NODES=$($computeEntries -join ',')"
)
$envPath = Join-Path $RepoRoot '.env'
if ((Test-Path $envPath) -and -not ((Get-Content -Raw $envPath) -like '# Generated by deploy/*')) {
    $backup = "$envPath.bak-$(Get-Date -Format yyyyMMddHHmmss)"
    Copy-Item $envPath $backup
    Warn "原有 .env 已备份为 $backup"
}
Write-Utf8NoBom $envPath (($envLines -join "`r`n") + "`r`n")
Ok $envPath

Step "防火墙放行 TCP $port"
Remove-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $FirewallRuleName -Direction Inbound -Protocol TCP -LocalPort $port `
    -Action Allow -Profile Any | Out-Null
Ok '已放行'

Step '关闭插电时的睡眠和休眠'
Invoke-Native 'powercfg.exe' @('/change', 'standby-timeout-ac', '0') | Out-Null
Invoke-Native 'powercfg.exe' @('/change', 'hibernate-timeout-ac', '0') | Out-Null
Ok '已关闭'

Step '配置 SSH 免密登录'
$ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
if (-not (Test-Path $ssh)) {
    Write-Host '    安装 Windows 自带的 OpenSSH 客户端...'
    Add-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0' | Out-Null
}
if (-not (Test-Path $ssh)) { throw '未能安装 OpenSSH 客户端' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $KeyFile) | Out-Null
if (-not (Test-Path $KeyFile)) {
    Start-Process -FilePath (Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe') -Wait -NoNewWindow `
        -ArgumentList "-t ed25519 -q -N `"`" -C 3dr-$NodeId -f `"$KeyFile`""
    Ok "已生成密钥 $KeyFile"
}
# Files created from an elevated shell are owned by Administrators; the non-elevated task must own the key,
# and OpenSSH rejects private keys readable by anyone else.
$me = "$env:USERDOMAIN\$env:USERNAME"
Invoke-Native 'icacls.exe' @($KeyFile, '/setowner', $me) | Out-Null
Invoke-Native 'icacls.exe' @($KeyFile, '/inheritance:r', '/grant:r', "${me}:F") | Out-Null
$publicKey = (Get-Content -Raw "$KeyFile.pub").Trim()
$knownHosts = Join-Path (Split-Path -Parent $KeyFile) 'known_hosts'
$sshCommon = @('-i', $KeyFile, '-o', 'StrictHostKeyChecking=accept-new', '-o', "UserKnownHostsFile=$knownHosts",
    '-o', 'ConnectTimeout=10')
foreach ($h in $cfg.ssh_hosts) {
    $target = "$($h.user)@$($h.host)"
    $probe = Invoke-Native $ssh ($sshCommon + @('-o', 'BatchMode=yes', '-p', "$($h.port)", $target, 'exit'))
    if ($probe.ExitCode -eq 0) { Ok "$($h.name) 已可免密登录"; continue }
    Warn "$($h.name) ($target) 还不能免密登录，接下来需要输入一次该服务器的密码"
    $install = "umask 077; mkdir -p ~/.ssh; grep -qxF '$publicKey' ~/.ssh/authorized_keys 2>/dev/null || echo '$publicKey' >> ~/.ssh/authorized_keys"
    Invoke-Native $ssh ($sshCommon + @('-p', "$($h.port)", $target, $install)) -Stream | Out-Null
    $probe = Invoke-Native $ssh ($sshCommon + @('-o', 'BatchMode=yes', '-p', "$($h.port)", $target, 'exit'))
    if ($probe.ExitCode -eq 0) { Ok "$($h.name) 免密登录已配置" } else { Fail "$($h.name) 免密登录仍失败：$($probe.Output)" }
}

Step '注册并启动计划任务（开机自启、崩溃自动重启）'
Get-ScheduledTask -TaskName "$TaskPrefix*" -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false
Register-3drTask $TransferTaskName (Join-Path $PSScriptRoot 'run_transfer.ps1') "-Port $port"
Ok $TransferTaskName
foreach ($h in $cfg.ssh_hosts) {
    $forwards = ($h.compute_nodes | ForEach-Object { "$($_.local_port):$($_.remote_port)" }) -join ','
    $taskName = "${TaskPrefix}Tunnel-$($h.name)"
    $taskArgs = "-Name $($h.name) -Target $($h.user)@$($h.host) -SshPort $($h.port) -KeyFile `"$KeyFile`" " +
        "-LocalForwards $forwards -CallbackPort $CallbackPort -TransferPort $port"
    Register-3drTask $taskName (Join-Path $PSScriptRoot 'run_tunnel.ps1') $taskArgs
    Ok $taskName
}

Step '自检（等待服务启动）'
$headers = @{ 'X-Internal-Key' = $cfg.internal_api_key }
$healthy = $false
for ($i = 0; $i -lt 30 -and -not $healthy; $i++) {
    Start-Sleep -Seconds 2
    try { $healthy = (Invoke-RestMethod -TimeoutSec 3 "http://127.0.0.1:$port/health").status -eq 'ok' } catch {}
}
if ($healthy) { Ok "中转服务 http://127.0.0.1:$port/health 正常" }
else { Fail "中转服务没有起来，查看 $RepoRoot\logs\transfer.log" }

foreach ($h in $cfg.ssh_hosts) {
    foreach ($n in $h.compute_nodes) {
        if (Test-TcpPort '127.0.0.1' ([int]$n.local_port)) { Ok "隧道 $($h.name) -> $($n.node_id) 本地端口 $($n.local_port) 已打开" }
        else { Fail "隧道 $($h.name) -> $($n.node_id) 本地端口 $($n.local_port) 不通，查看 $RepoRoot\logs\tunnel-$($h.name).log" }
    }
    $tunnelLog = Join-Path $RepoRoot "logs\tunnel-$($h.name).log"
    if ((Test-Path $tunnelLog) -and (Select-String -Quiet -Path $tunnelLog -Pattern 'remote port forwarding failed')) {
        Fail "$($h.name) 上的回传端口 $CallbackPort 已被占用，换一个 -CallbackPort 重新部署"
    }
}

if ($healthy) {
    try {
        $availability = Invoke-RestMethod -TimeoutSec 20 -Headers $headers "http://127.0.0.1:$port/api/v1/compute-nodes/availability"
        foreach ($n in $availability.nodes) {
            if ($n.available) { Ok "计算节点 $($n.node_id) 可接单（$($n.active_jobs)/$($n.capacity)）" }
            else { Warn "计算节点 $($n.node_id) 不可用：$($n.reason)" }
        }
    } catch { Warn "查询计算节点可用性失败：$_" }
}

$registered = $false
for ($i = 0; $i -lt 10 -and -not $registered; $i++) {
    try {
        $nodes = Invoke-RestMethod -TimeoutSec 5 -Headers $headers "$($cfg.control_plane_url.TrimEnd('/'))/internal/nodes"
        $selfNode = $nodes | Where-Object { $_.node_id -eq $NodeId }
        if ($selfNode) {
            $registered = $true
            Ok "主控已收到心跳，登记的地址 $($selfNode.public_url)，最近心跳 $($selfNode.last_heartbeat) (UTC)"
        }
    } catch {}
    if (-not $registered) { Start-Sleep -Seconds 3 }
}
if (-not $registered) { Fail "主控 $($cfg.control_plane_url) 上没看到 $NodeId，检查本机能否访问主控、密钥是否一致" }

Write-Host ''
Write-Host "部署结束。日志目录：$RepoRoot\logs" -ForegroundColor Cyan
Write-Host "浏览器会通过 http://${PublicIp}:$port 上传和下载，请确认用户所在网络能访问这个地址。" -ForegroundColor Cyan
if (-not $known.ContainsKey($NodeId)) {
    Write-Host "别忘了把 `"$NodeId`": { `"callback_port`": $CallbackPort } 写回共享的 cluster.json。" -ForegroundColor Yellow
}
