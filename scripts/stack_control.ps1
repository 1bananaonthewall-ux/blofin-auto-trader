param(
    [ValidateSet("start", "stop", "stop-stack", "restart", "restart-fresh", "status", "ensure")]
    [string]$Action = "status",
    [switch]$RunHourlyNow,
    [int]$DashboardPort = 5050
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$BotTask = "BlofinLiveBot"
$HourlyTask = "BlofinHourlyMaintain"
$StackGuardTask = "BlofinStackGuard"
$BotPy = Join-Path $Root "bot.py"
$HourlyPs1 = Join-Path $Root "run_hourly.ps1"
$Py = if (Test-Path ".\.venv\Scripts\python.exe") { Resolve-Path ".\.venv\Scripts\python.exe" } else { (Get-Command python).Source }

function Resolve-BotPython {
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        return (Resolve-Path $venvPy).Path
    }
    try {
        $real = (& $Py -c "import sys; print(sys._base_executable)" 2>&1 | Out-String).Trim()
        if ($real -and (Test-Path $real) -and $real -notmatch '\\\.venv\\Scripts\\python\.exe$') {
            return $real
        }
    } catch { }
    $candidates = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object { $_.FullName } -Descending
    if ($candidates) {
        return $candidates[0].FullName
    }
    return $Py
}

$BotPython = Resolve-BotPython
$BotPidFile = Join-Path $Root "state\bot.pid"
$EnsureLockFile = Join-Path $Root "state\stack_ensure.lock"

function Write-BotPidFile {
    param([int]$ProcessId)
    New-Item -ItemType Directory -Force -Path (Split-Path $BotPidFile) | Out-Null
    "$ProcessId" | Out-File $BotPidFile -Encoding ascii -NoNewline
}

function Clear-BotPidFile {
    if (Test-Path $BotPidFile) {
        Remove-Item $BotPidFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-PidFileBotProcess {
    if (-not (Test-Path $BotPidFile)) { return $null }
    $savedPid = 0
    try { $savedPid = [int](Get-Content $BotPidFile -ErrorAction Stop | Select-Object -First 1) } catch { return $null }
    if ($savedPid -le 0) {
        Clear-BotPidFile
        return $null
    }
    $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
    if (-not $proc -or $proc.ProcessName -ne "python") {
        Clear-BotPidFile
        return $null
    }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
}

function Test-BotProcessAlive {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $false }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    return ($null -ne $proc -and $proc.ProcessName -eq "python")
}

function Enter-EnsureLock {
    New-Item -ItemType Directory -Force -Path (Split-Path $EnsureLockFile) | Out-Null
    if (Test-Path $EnsureLockFile) {
        $age = ((Get-Date) - (Get-Item $EnsureLockFile).LastWriteTime).TotalSeconds
        if ($age -lt 180) {
            Write-Host "Ensure lock active (${age}s) - skipping concurrent ensure"
            return $false
        }
        Remove-Item $EnsureLockFile -Force -ErrorAction SilentlyContinue
    }
    (Get-Date).ToString("o") | Out-File $EnsureLockFile -Encoding ascii
    return $true
}

function Exit-EnsureLock {
    if (Test-Path $EnsureLockFile) {
        Remove-Item $EnsureLockFile -Force -ErrorAction SilentlyContinue
    }
}

function Test-IsOurBotProcess {
    param([string]$CommandLine)
    if (-not $CommandLine -or $CommandLine -notmatch "bot\.py") { return $false }
    if ($CommandLine -match "hourly_maintain|dashboard_api|test_dashboard|godbot_audit|cortex_smoke|dashboard_copilot|stack_status") {
        return $false
    }
    $norm = $CommandLine.Replace('/', '\')
    $rootNorm = $Root.Replace('/', '\')
    if ($norm -like "*$rootNorm*") { return $true }
    # Stray system-python "python.exe bot.py" started from project cwd (no root in WMI cmdline).
    if ($norm -match 'python\.exe"?\s+bot\.py(\s|$)') { return $true }
    return $false
}

function Get-AllBotPyProcesses {
    $found = @{}
    $raw = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        Test-IsOurBotProcess $_.CommandLine
    })
    $pidSet = @{}
    foreach ($b in $raw) { $pidSet[[int]$b.ProcessId] = $true }
    foreach ($b in $raw) {
        # bot.py may spawn a child system-python worker; keep only the root process.
        $parentId = [int]$b.ParentProcessId
        if ($parentId -gt 0 -and $pidSet.ContainsKey($parentId)) { continue }
        $found[$b.ProcessId] = $b
    }
    $pidBot = Get-PidFileBotProcess
    if ($pidBot -and -not $found.ContainsKey($pidBot.ProcessId)) {
        $found[$pidBot.ProcessId] = $pidBot
    }
    @($found.Values)
}

function Stop-ConcurrentStackScripts {
    $myPid = $PID
    @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
        $_.ProcessId -ne $myPid -and $_.CommandLine -match "stack_guard\.ps1|stack_control\.ps1|run_god_bot_stack\.ps1"
    }) | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped concurrent stack script pid $($_.ProcessId)"
    }
    Start-Sleep -Seconds 1
}

function Stop-AllBots {
    param([int]$MaxWaitSec = 20)
    $stopped = 0
    $pidBot = Get-PidFileBotProcess
    if ($pidBot) {
        Stop-Process -Id $pidBot.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped bot pid $($pidBot.ProcessId) (pid file)"
        $stopped++
    }
    Clear-BotPidFile
    for ($round = 0; $round -lt 6; $round++) {
        $bots = @(Get-AllBotPyProcesses)
        if ($bots.Count -eq 0) { break }
        foreach ($b in $bots) {
            Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped bot pid $($b.ProcessId)"
            $stopped++
        }
        Start-Sleep -Seconds 1
    }
    $left = @(Get-AllBotPyProcesses)
    if ($left.Count -gt 0) {
        Write-Warning "Still $($left.Count) bot process(es) after stop - retrying"
        foreach ($b in $left) {
            Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
    }
    return $stopped
}

function Stop-DashboardApi {
    param([int]$ListenPort)
    $pids = @(
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object {
                $cmd = $_.CommandLine
                $cmd -and $cmd -match "dashboard_api\.py" -and $cmd -like "*$Root*"
            } |
            Select-Object -ExpandProperty ProcessId
    )
    foreach ($listenPid in (
        Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )) {
        if ($listenPid -gt 0) { $pids += $listenPid }
    }
    foreach ($procId in ($pids | Select-Object -Unique)) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped dashboard pid $procId"
    }
    if ($pids.Count -gt 0) {
        Start-Sleep -Seconds 1
    }
}

function Start-CurveGuardDaemon {
    $pidFile = Join-Path $Root "state\curve_guard.pid"
    if (Test-Path $pidFile) {
        $savedPid = 0
        try { $savedPid = [int](Get-Content $pidFile -ErrorAction Stop | Select-Object -First 1) } catch { }
        if ($savedPid -gt 0) {
            $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -eq "python") {
                return
            }
        }
    }
    $daemon = Join-Path $Root "scripts\curve_guard_daemon.py"
    if (-not (Test-Path $daemon)) { return }
    Start-Process -FilePath $BotPython -WindowStyle Hidden -ArgumentList @(
        $daemon
    ) -WorkingDirectory $Root | Out-Null
    Write-Host "Curve guard daemon started"
}

function Start-DashboardApi {
    param([int]$ListenPort)
    $dashPs1 = Join-Path $Root "scripts\start_dashboard_quiet.ps1"
    Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $dashPs1,
        "-Port", "$ListenPort"
    ) | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            if (@(Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
                Write-Host "Dashboard listening on port $ListenPort"
                return $true
            }
        } catch { }
    }
    Write-Warning "Dashboard start requested but port $ListenPort not listening yet"
    return $false
}

function Test-BotLogAlive {
    param([int]$MaxAgeSec = 45)
    $pidBot = Get-PidFileBotProcess
    if ($pidBot) { return $true }
    $log = Join-Path $Root "logs\bot.log"
    if (-not (Test-Path $log)) { return $false }
    if (((Get-Date) - (Get-Item $log).LastWriteTime).TotalSeconds -gt $MaxAgeSec) { return $false }
    $tail = Get-Content $log -Tail 40 -ErrorAction SilentlyContinue
    return ($tail -match "AUTONOMOUS ENGINE|equity=\$|LLM OVERSEER|seeded equity cache")
}

function Wait-ForSingleBot {
    # HF local LLM warmup can take 60-120s before bot.py shows in WMI CommandLine.
    param([int]$MaxSec = 120)
    for ($i = 0; $i -lt $MaxSec; $i++) {
        Start-Sleep -Seconds 1
        $bots = @(Get-BotProcesses)
        if ($bots.Count -gt 1 -and $i -ge 45) {
            Stop-DuplicateBots | Out-Null
            $bots = @(Get-BotProcesses)
        }
        if ($bots.Count -ge 1) {
            Write-Host "Started bot pid $($bots[0].ProcessId) (detected after ${i}s)"
            return $bots[0].ProcessId
        }
        if ($i -ge 15 -and (Test-BotLogAlive)) {
            $bots = @(Get-BotProcesses)
            if ($bots.Count -ge 1) {
                Write-Host "Started bot pid $($bots[0].ProcessId) (log activity after ${i}s)"
                return $bots[0].ProcessId
            }
            $pidBot = Get-PidFileBotProcess
            if ($pidBot) {
                Write-Host "Started bot pid $($pidBot.ProcessId) (pid file + log after ${i}s)"
                return $pidBot.ProcessId
            }
        }
    }
    return $null
}

function Start-BotProcess {
    Stop-AllBots | Out-Null
    Start-Sleep -Seconds 2
    $env:PYTHONUNBUFFERED = "1"
    $proc = Start-Process -FilePath $BotPython -ArgumentList @($BotPy) -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Write-BotPidFile $proc.Id
    return $proc.Id
}

function Restart-FreshStack {
    Write-Host "=== Fresh stack restart (kill all bots + dashboard, start clean) ===" -ForegroundColor Cyan
    Ensure-TaskDisabled $BotTask
    Ensure-TaskDisabled $StackGuardTask
    $n = Stop-AllBots
    Write-Host "Stopped $n bot process(es)"
    Write-Host "Bot worker: $BotPython"
    Stop-DashboardApi -ListenPort $DashboardPort
    Start-Sleep -Seconds 5
    Stop-AllBots | Out-Null
    Start-BotProcess | Out-Null
    $botPid = Wait-ForSingleBot -MaxSec 120
    if (-not $botPid) {
        Write-Warning "First bot start not detected after 120s - retrying once"
        Stop-AllBots | Out-Null
        Start-Sleep -Seconds 3
        Start-BotProcess | Out-Null
        $botPid = Wait-ForSingleBot -MaxSec 90
    }
    if (-not $botPid) {
        Write-Warning "Bot start requested, but process was not detected."
    }
    Start-DashboardApi -ListenPort $DashboardPort | Out-Null
    Start-CurveGuardDaemon
    & $BotPython (Join-Path $Root "scripts\curve_guard_daemon.py") --once 2>&1 | Out-Null
    Start-Sleep -Seconds 8
    $pidBot = Get-PidFileBotProcess
    if (-not $pidBot -and -not (Test-BotLogAlive)) {
        Write-Warning "Bot not healthy after dashboard start - running ensure"
        Ensure-SingleInstance
    }
    Start-Sleep -Seconds 2
    Ensure-TaskEnabled $StackGuardTask
    Ensure-TaskEnabled $HourlyTask
    if ($RunHourlyNow -and (Test-Path $HourlyPs1)) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $HourlyPs1
    }
    Show-Status
    Write-Host "Dashboard: http://127.0.0.1:$DashboardPort" -ForegroundColor Green
}

function Get-BotProcesses {
    Get-AllBotPyProcesses
}

function Test-SchedulerTasksDisabled {
    Test-Path (Join-Path $Root "state\no_scheduler_tasks")
}

function Ensure-TaskEnabled([string]$TaskName) {
    if (Test-SchedulerTasksDisabled) { return }
    schtasks /Change /TN $TaskName /ENABLE 2>&1 | Out-Null
}

function Ensure-TaskDisabled([string]$TaskName) {
    schtasks /Change /TN $TaskName /DISABLE 2>&1 | Out-Null
}

function Stop-Bot {
    $n = Stop-AllBots
    if ($n -eq 0) {
        Write-Host "Bot: already stopped"
    }
}

function Start-Bot {
    $pidBot = Get-PidFileBotProcess
    if ($pidBot) {
        Write-Host "Bot already running pid $($pidBot.ProcessId) (pid file)"
        return $true
    }
    $bots = @(Get-BotProcesses)
    if ($bots.Count -gt 1) {
        Stop-DuplicateBots | Out-Null
        Start-Sleep -Seconds 1
        $bots = @(Get-BotProcesses)
    }
    if ($bots.Count -ge 1) {
        Write-Host "Bot already running pid $($bots[0].ProcessId)"
        Write-BotPidFile $bots[0].ProcessId
        return $true
    }
    Start-BotProcess | Out-Null
    $botPid = Wait-ForSingleBot -MaxSec 120
    if ($botPid) {
        return $true
    }
    Write-Warning "Bot start requested, but process was not detected."
    return $false
}

function Select-BotProcessToKeep {
    param([array]$Bots)
    if ($Bots.Count -eq 0) { return $null }
    # Always keep the venv worker; system-python bot.py duplicates are stale.
    $venv = @($Bots | Where-Object { $_.CommandLine -like "*\.venv\Scripts\python.exe*" })
    if ($venv.Count -ge 1) {
        return $venv | Sort-Object ProcessId -Descending | Select-Object -First 1
    }
    $pidBot = Get-PidFileBotProcess
    if ($pidBot) {
        $match = @($Bots | Where-Object { $_.ProcessId -eq $pidBot.ProcessId })
        if ($match.Count -ge 1) { return $match[0] }
    }
    return $Bots | Sort-Object ProcessId -Descending | Select-Object -First 1
}

function Stop-DuplicateBots {
    $bots = @(Get-BotProcesses)
    if ($bots.Count -le 1) {
        return $bots
    }
    $keep = Select-BotProcessToKeep $bots
    foreach ($b in $bots) {
        if ($b.ProcessId -ne $keep.ProcessId) {
            Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped duplicate bot pid $($b.ProcessId)"
        }
    }
    Write-Host "Single bot kept pid $($keep.ProcessId)"
    Write-BotPidFile $keep.ProcessId
    Start-Sleep -Seconds 1
    if (Test-BotProcessAlive $keep.ProcessId) {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($keep.ProcessId)" -ErrorAction SilentlyContinue
        if ($cim) { return @($cim) }
    }
    Clear-BotPidFile
    $left = @(Get-BotProcesses)
    if ($left.Count -ge 1) {
        $retry = Select-BotProcessToKeep $left
        if ($retry) {
            Write-BotPidFile $retry.ProcessId
            Write-Host "Recovered bot pid $($retry.ProcessId) after dedup"
            return @($retry)
        }
    }
    return $left
}

function Keep-SingleBot {
    $bots = @(Get-BotProcesses)
    if ($bots.Count -eq 0) { return $null }
    if ($bots.Count -eq 1) { return $bots[0].ProcessId }
    return (Stop-DuplicateBots | Select-Object -First 1).ProcessId
}

function Stop-StraySystemBots {
    $bots = @(Get-BotProcesses)
    $venvBots = @($bots | Where-Object { $_.CommandLine -like "*\.venv\Scripts\python.exe*" })
    $systemBots = @($bots | Where-Object { $_.CommandLine -notlike "*\.venv\Scripts\python.exe*" })
    if ($venvBots.Count -ge 1 -and $systemBots.Count -ge 1) {
        foreach ($b in $systemBots) {
            Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped stray system bot pid $($b.ProcessId)"
        }
        Start-Sleep -Seconds 1
    }
}

function Ensure-SingleInstance {
    if (-not (Enter-EnsureLock)) {
        Show-Status
        return
    }
    # Disable tasks that can spawn a second bot.py during ensure/restart.
    Ensure-TaskDisabled $BotTask
    Ensure-TaskDisabled $StackGuardTask
    Stop-ConcurrentStackScripts
    try {
        Stop-StraySystemBots
        if ((Get-BotProcesses).Count -gt 1) {
            Stop-DuplicateBots | Out-Null
            Start-Sleep -Seconds 2
        }
        $pidBot = Get-PidFileBotProcess
        if ($pidBot) {
            Write-Host "Single bot ok pid $($pidBot.ProcessId) (pid file)"
            return
        }
        $bots = @(Get-BotProcesses)
        if ($bots.Count -eq 1) {
            Write-BotPidFile $bots[0].ProcessId
            Write-Host "Single bot ok pid $($bots[0].ProcessId)"
            return
        }
        if ($bots.Count -gt 1) {
            Stop-DuplicateBots | Out-Null
            Start-Sleep -Seconds 2
            $pidBot = Get-PidFileBotProcess
            if ($pidBot) {
                Write-Host "Single bot ok pid $($pidBot.ProcessId) (pid file)"
                return
            }
            $bots = @(Get-BotProcesses)
            if ($bots.Count -eq 1) {
                Write-BotPidFile $bots[0].ProcessId
                Write-Host "Single bot ok pid $($bots[0].ProcessId)"
                return
            }
        }
        if (Test-BotLogAlive) {
            $pidBot = Get-PidFileBotProcess
            if ($pidBot) {
                Write-Host "Single bot ok pid $($pidBot.ProcessId) (warmup)"
                return
            }
            $bots = @(Get-BotProcesses)
            if ($bots.Count -ge 1) {
                Write-BotPidFile $bots[0].ProcessId
                Write-Host "Single bot ok pid $($bots[0].ProcessId) (warmup)"
                return
            }
        }
        Write-Host "No bot running - starting one instance"
        if (-not (Start-Bot)) {
            Write-Warning "Start failed - retrying once"
            Start-Sleep -Seconds 5
            Start-Bot | Out-Null
        }
        $pidBot = Get-PidFileBotProcess
        if ($pidBot -and (Test-BotProcessAlive $pidBot.ProcessId)) {
            Write-Host "Single bot ok pid $($pidBot.ProcessId)"
        } elseif ((Get-BotProcesses).Count -gt 1) {
            Stop-DuplicateBots | Out-Null
            Start-Sleep -Seconds 2
            $pidBot = Get-PidFileBotProcess
            if ($pidBot) {
                Write-Host "Single bot ok pid $($pidBot.ProcessId)"
            }
        }
    } finally {
        Start-Sleep -Seconds 2
        Ensure-TaskEnabled $StackGuardTask
        Ensure-TaskEnabled $HourlyTask
        Exit-EnsureLock
    }
}

function Show-Status {
    Write-Host "=== Bot ==="
    $bots = @(Get-BotProcesses)
    if ($bots.Count -eq 0) {
        $pidBot = Get-PidFileBotProcess
        if ($pidBot) { $bots = @($pidBot) }
    }
    if ($bots.Count -eq 0 -and (Test-BotLogAlive)) {
        Write-Host "bot.py: WARMUP (log active, pid pending)"
    } elseif ($bots.Count -eq 0) {
        Write-Host "bot.py: NOT RUNNING"
    } else {
        foreach ($b in $bots) {
            $venv = if ($b.CommandLine -like "*.venv*") { "venv" } else { "system" }
            Write-Host "bot.py: RUNNING pid=$($b.ProcessId) ($venv)"
        }
    }

    Write-Host ""
    Write-Host "=== Scheduled Tasks ==="
    foreach ($tn in @($BotTask, $HourlyTask, $StackGuardTask)) {
        $q = schtasks /Query /TN $tn /FO LIST 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "${tn}: NOT FOUND"
            continue
        }
        $status = ($q | Select-String "Status:").ToString().Trim()
        $nextRun = ($q | Select-String "Next Run Time:").ToString().Trim()
        Write-Host ("{0} | {1} | {2}" -f $tn, $status, $nextRun)
    }
}

switch ($Action) {
    "stop" {
        Ensure-TaskDisabled $BotTask
        Stop-Bot
        Show-Status
    }
    "stop-stack" {
        Ensure-TaskDisabled $BotTask
        Ensure-TaskDisabled $StackGuardTask
        Stop-Bot
        Stop-DashboardApi -ListenPort $DashboardPort
        Show-Status
    }
    "start" {
        Ensure-SingleInstance
        if ($RunHourlyNow -and (Test-Path $HourlyPs1)) {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $HourlyPs1
        }
        Show-Status
    }
    "restart" {
        Restart-FreshStack
    }
    "restart-fresh" {
        Restart-FreshStack
    }
    "status" {
        Show-Status
    }
    "ensure" {
        Ensure-SingleInstance
        Start-CurveGuardDaemon
        & $BotPython (Join-Path $Root "scripts\curve_guard_daemon.py") --once 2>&1 | Out-Null
        Show-Status
    }
}
