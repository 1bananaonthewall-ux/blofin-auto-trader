param(
    [ValidateSet("start", "stop", "restart", "restart-fresh", "status", "ensure")]
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

function Test-IsOurBotProcess {
    param([string]$CommandLine)
    if (-not $CommandLine -or $CommandLine -notmatch "bot\.py") { return $false }
    if ($CommandLine -match "hourly_maintain|dashboard_api|test_dashboard|godbot_audit|cortex_smoke|dashboard_copilot") {
        return $false
    }
    return $CommandLine -like "*$Root*"
}

function Stop-AllBots {
    param([int]$MaxWaitSec = 20)
    $stopped = 0
    for ($round = 0; $round -lt 6; $round++) {
        $bots = @(Get-BotProcesses)
        if ($bots.Count -eq 0) { break }
        foreach ($b in $bots) {
            Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped bot pid $($b.ProcessId)"
            $stopped++
        }
        Start-Sleep -Seconds 1
    }
    $left = @(Get-BotProcesses)
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

function Start-DashboardApi {
    param([int]$ListenPort)
    $dashPs1 = Join-Path $Root "scripts\run_dashboard.ps1"
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

function Wait-ForSingleBot {
    param([int]$MaxSec = 25)
    for ($i = 0; $i -lt $MaxSec; $i++) {
        Start-Sleep -Seconds 1
        $bots = @(Get-BotProcesses)
        if ($bots.Count -gt 1) {
            Keep-SingleBot | Out-Null
            $bots = @(Get-BotProcesses)
        }
        if ($bots.Count -ge 1) {
            Write-Host "Started bot pid $($bots[0].ProcessId)"
            return $bots[0].ProcessId
        }
    }
    return $null
}

function Restart-FreshStack {
    Write-Host "=== Fresh stack restart (kill all bots + dashboard, start clean) ===" -ForegroundColor Cyan
    Ensure-TaskDisabled $BotTask
    $n = Stop-AllBots
    Write-Host "Stopped $n bot process(es)"
    Write-Host "Bot worker: $BotPython"
    Stop-DashboardApi -ListenPort $DashboardPort
    Start-Sleep -Seconds 5
    Stop-AllBots | Out-Null
    $env:PYTHONUNBUFFERED = "1"
    Start-Process -FilePath $BotPython -ArgumentList @($BotPy) -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
    $botPid = Wait-ForSingleBot
    if (-not $botPid) {
        Write-Warning "First bot start not detected - retrying"
        Stop-AllBots | Out-Null
        Start-Sleep -Seconds 2
        Start-Process -FilePath $BotPython -ArgumentList @($BotPy) -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
        $botPid = Wait-ForSingleBot
    }
    if (-not $botPid) {
        Write-Warning "Bot start requested, but process was not detected."
    }
    Start-DashboardApi -ListenPort $DashboardPort | Out-Null
    Start-Sleep -Seconds 2
    Keep-SingleBot | Out-Null
    if ($RunHourlyNow -and (Test-Path $HourlyPs1)) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $HourlyPs1
    }
    Show-Status
    Write-Host "Dashboard: http://127.0.0.1:$DashboardPort" -ForegroundColor Green
}

function Get-BotProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        Test-IsOurBotProcess $_.CommandLine
    })
}

function Ensure-TaskEnabled([string]$TaskName) {
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
    $bots = @(Get-BotProcesses)
    if ($bots.Count -gt 1) {
        Stop-AllBots | Out-Null
        Start-Sleep -Seconds 2
        $bots = @(Get-BotProcesses)
    }
    if ($bots.Count -ge 1) {
        Write-Host "Bot already running pid $($bots[0].ProcessId)"
        return $true
    }
    $env:PYTHONUNBUFFERED = "1"
    Start-Process -FilePath $BotPython -ArgumentList @($BotPy) -WorkingDirectory $Root -WindowStyle Hidden
    for ($i = 0; $i -lt 12; $i++) {
        Start-Sleep -Seconds 1
        $started = @(Get-BotProcesses)
        if ($started.Count -ge 1) {
            if ($started.Count -gt 1) {
                Stop-DuplicateBots | Out-Null
                $started = @(Get-BotProcesses)
            }
            if ($started.Count -ge 1) {
                Write-Host "Started bot pid $($started[0].ProcessId)"
                return $true
            }
        }
    }
    Write-Warning "Bot start requested, but process was not detected."
    return $false
}

function Select-BotProcessToKeep {
    param([array]$Bots)
    if ($Bots.Count -eq 0) { return $null }
    $worker = @($Bots | Where-Object { $_.CommandLine -notmatch '\\\.venv\\Scripts\\python\.exe"' })
    if ($worker.Count -ge 1) {
        return $worker | Sort-Object ProcessId -Descending | Select-Object -First 1
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
    return @(Get-BotProcesses)
}

function Keep-SingleBot {
    $bots = @(Get-BotProcesses)
    if ($bots.Count -eq 0) { return $null }
    if ($bots.Count -eq 1) { return $bots[0].ProcessId }
    return (Stop-DuplicateBots | Select-Object -First 1).ProcessId
}

function Ensure-SingleInstance {
    # Only disable BlofinLiveBot — it can spawn a second bot.py.
    # StackGuard + HourlyMaintain are safe (ensure + maintain scripts, not duplicate bots).
    Ensure-TaskDisabled $BotTask
    if ((Get-BotProcesses).Count -gt 1) {
        Stop-DuplicateBots | Out-Null
        Start-Sleep -Seconds 1
    }
    $bots = @(Get-BotProcesses)
    if ($bots.Count -eq 1) {
        Write-Host "Single bot ok pid $($bots[0].ProcessId)"
        return
    }
    if ($bots.Count -gt 1) {
        Stop-DuplicateBots | Out-Null
        Start-Sleep -Seconds 1
        $bots = @(Get-BotProcesses)
        if ($bots.Count -eq 1) {
            Write-Host "Single bot ok pid $($bots[0].ProcessId)"
            return
        }
    }
    Write-Host "No bot running - starting one instance"
    if (-not (Start-Bot)) {
        Write-Warning "Start failed - retrying once"
        Start-Sleep -Seconds 3
        Start-Bot | Out-Null
    }
}

function Show-Status {
    Write-Host "=== Bot ==="
    $bots = Get-BotProcesses
    if ($bots.Count -eq 0) {
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
    "start" {
        Ensure-TaskDisabled $BotTask
        Start-Bot | Out-Null
        if ($RunHourlyNow -and (Test-Path $HourlyPs1)) {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $HourlyPs1
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
        Show-Status
    }
}
