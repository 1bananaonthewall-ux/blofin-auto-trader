# Start/stop fully autonomous stack (bot + hustle daemon) — no user action required after launch
param(
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$DaemonPidFile = "state\hustle_daemon.pid"
$BotPidFile = "state\bot.pid"

function Stop-ProcFromFile($path) {
    if (-not (Test-Path $path)) { return }
    $pid = Get-Content $path -ErrorAction SilentlyContinue
    if ($pid) {
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $path -Force -ErrorAction SilentlyContinue
}

if ($Stop) {
    Stop-ProcFromFile $DaemonPidFile
    Stop-ProcFromFile $BotPidFile
    Write-Host "Stopped autonomous processes."
    exit 0
}

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    .\.venv\Scripts\pip install -q -r requirements.txt
}

# Hustle daemon (treasury + equity log + ensures bot)
$daemonLog = "logs\hustle_daemon_boot.log"
$daemon = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList "hustle_daemon.py" `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput $daemonLog `
    -RedirectStandardError $daemonLog

$daemon.Id | Out-File -Encoding utf8 $DaemonPidFile
Write-Host "Autonomous stack running."
Write-Host "  hustle_daemon pid=$($daemon.Id)  (interval 300s, starts bot if down)"
Write-Host "  logs: logs\hustle_daemon.log, logs\bot.log, state\hustle_report.jsonl"
Write-Host "  stop: .\run_autonomous.ps1 -Stop"
Write-Host ""
Write-Host "See AUTONOMOUS.md - real payouts still need capital, deposits, or winning trades."
