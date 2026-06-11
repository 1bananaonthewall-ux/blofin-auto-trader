# Register Windows Task Scheduler — God Bot caretaker every 10 min + enable stack guard.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$CaretakerPs1 = Join-Path $Root "scripts\god_bot_caretaker.ps1"
$GuardPs1 = Join-Path $Root "scripts\stack_guard.ps1"
$TaskName = "BlofinGodBotCaretaker"
$GuardTask = "BlofinStackGuard"

if (-not (Test-Path $CaretakerPs1)) {
    Write-Error "Missing $CaretakerPs1"
    exit 1
}

$action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$CaretakerPs1`""
schtasks /Create /F /TN $TaskName /SC MINUTE /MO 10 /TR $action /RL LIMITED 2>&1 | Out-Host
schtasks /Change /TN $TaskName /ENABLE 2>&1 | Out-Host

Write-Host "Registered $TaskName (every 10 min)" -ForegroundColor Green
schtasks /Query /TN $TaskName /FO LIST | Select-String "Status|Next Run|Task To Run"

if (Test-Path $GuardPs1) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\install_stack_guard.ps1")
}

Write-Host "Running caretaker once now..." -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $CaretakerPs1
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stack_control.ps1") -Action status
