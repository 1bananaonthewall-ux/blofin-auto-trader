# Register Windows Task Scheduler watchdog — keeps God Bot + dashboard alive every 5 min.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$GuardPs1 = Join-Path $Root "scripts\stack_guard.ps1"
$TaskName = "BlofinStackGuard"

if (-not (Test-Path $GuardPs1)) {
    Write-Error "Missing $GuardPs1"
    exit 1
}

$action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$GuardPs1`""
schtasks /Create /F /TN $TaskName /SC MINUTE /MO 5 /TR $action /RL LIMITED 2>&1 | Out-Host
schtasks /Change /TN $TaskName /ENABLE 2>&1 | Out-Host

Write-Host "Registered $TaskName (every 5 min)" -ForegroundColor Green
schtasks /Query /TN $TaskName /FO LIST | Select-String "Status|Next Run|Task To Run"

# Run once now
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GuardPs1
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stack_control.ps1") -Action status
