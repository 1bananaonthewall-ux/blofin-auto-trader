# Silent boot: single venv bot + dashboard on :5050 (logon task + manual recovery).
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "boot_stack.log"

function Write-BootLog {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $Message" | Out-File $Log -Append -Encoding utf8
}

Write-BootLog "boot_god_bot_stack start"

# BlofinLiveBot spawns a second bot.py — keep disabled; stack_control owns the worker.
schtasks /Change /TN "BlofinLiveBot" /DISABLE 2>&1 | Out-Null
schtasks /Change /TN "BlofinStackGuard" /DISABLE 2>&1 | Out-Null

$stackPs1 = Join-Path $Root "scripts\stack_control.ps1"
$ensureLock = Join-Path $Root "state\stack_ensure.lock"
for ($wait = 0; $wait -lt 30; $wait++) {
    if (-not (Test-Path $ensureLock)) { break }
    $age = ((Get-Date) - (Get-Item $ensureLock).LastWriteTime).TotalSeconds
    if ($age -ge 180) { break }
    Start-Sleep -Seconds 2
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $stackPs1 -Action ensure 2>&1 |
    Out-File $Log -Append -Encoding utf8

$dashPs1 = Join-Path $Root "scripts\start_dashboard_quiet.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $dashPs1 -Port 5050 2>&1 |
    Out-File $Log -Append -Encoding utf8

$noScheduler = Join-Path $Root "state\no_scheduler_tasks"
if (-not (Test-Path $noScheduler)) {
    schtasks /Change /TN "BlofinStackGuard" /ENABLE 2>&1 | Out-Null
    schtasks /Change /TN "BlofinHourlyMaintain" /ENABLE 2>&1 | Out-Null
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $stackPs1 -Action status 2>&1 |
    Out-File $Log -Append -Encoding utf8

Write-BootLog "boot_god_bot_stack done"
