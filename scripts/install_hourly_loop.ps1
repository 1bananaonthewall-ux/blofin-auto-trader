# Register Windows Task Scheduler jobs for hands-off God Bot maintenance.
#   BlofinStackGuard     — every 5 min: single bot, dashboard, TPSL repair
#   BlofinHourlyMaintain — every hour: 50x compliance, optimizer, cortex
#
# Run once:  powershell -File scripts\install_hourly_loop.ps1
# Status:    powershell -File scripts\stack_control.ps1 -Action status

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$HourlyPs1 = Join-Path $Root "run_hourly.ps1"
$GuardPs1 = Join-Path $Root "scripts\stack_guard.ps1"

if (-not (Test-Path $HourlyPs1)) { Write-Error "Missing $HourlyPs1" }
if (-not (Test-Path $GuardPs1)) { Write-Error "Missing $GuardPs1" }

$hourlyAction = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$HourlyPs1`""
$guardAction = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$GuardPs1`""

Write-Host "=== Installing God Bot automation tasks ===" -ForegroundColor Cyan

schtasks /Create /F /TN "BlofinHourlyMaintain" /SC HOURLY /MO 1 /TR $hourlyAction /RL LIMITED 2>&1 | Out-Host
schtasks /Change /TN "BlofinHourlyMaintain" /ENABLE 2>&1 | Out-Host
Write-Host "Registered BlofinHourlyMaintain (every hour)" -ForegroundColor Green

schtasks /Create /F /TN "BlofinStackGuard" /SC MINUTE /MO 5 /TR $guardAction /RL LIMITED 2>&1 | Out-Host
schtasks /Change /TN "BlofinStackGuard" /ENABLE 2>&1 | Out-Host
Write-Host "Registered BlofinStackGuard (every 5 min)" -ForegroundColor Green

Write-Host ""
Write-Host "Running first guard + hourly pass now..." -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GuardPs1
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $HourlyPs1

Write-Host ""
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stack_control.ps1") -Action status

Write-Host @"

Automation installed. You no longer need to prompt Cursor every hour for:
  - single bot enforcement (StackGuard, 5 min)
  - 50x compliance closes + optimizer + cortex (HourlyMaintain, 1 hr)

Cursor agent review still runs when you open the IDE if hourly is due
(.cursor/hooks/hourly_session_reminder.py + .cursor/HOURLY_DUE).

Optional cloud agent:  powershell -File scripts\setup_cursor_automation.ps1

"@ -ForegroundColor Yellow
