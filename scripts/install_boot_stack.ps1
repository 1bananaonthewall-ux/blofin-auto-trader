# One-shot: reboot, logon, God Bot + dashboard auto-start on :5050
# Run once from repo root: powershell -File scripts\install_boot_stack.ps1

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== God Bot boot stack installer ===" -ForegroundColor Cyan
Write-Host "Root: $Root"

$noScheduler = Join-Path $Root "state\no_scheduler_tasks"
if (Test-Path $noScheduler) {
    Remove-Item $noScheduler -Force
    Write-Host "Removed no_scheduler_tasks flag (re-enabling watchdog tasks)" -ForegroundColor Yellow
}

$BootPs1 = Join-Path $Root "scripts\boot_god_bot_stack.ps1"
$GuardInstall = Join-Path $Root "scripts\install_stack_guard.ps1"
$OpenDash = Join-Path $Root "scripts\open_god_bot_dashboard.ps1"
$BootTask = "BlofinGodBotBoot"

if (-not (Test-Path $BootPs1)) {
    Write-Error "Missing $BootPs1"
    exit 1
}

# Disable legacy logon bot task (duplicate worker).
schtasks /Change /TN "BlofinLiveBot" /DISABLE 2>&1 | Out-Null
Write-Host "BlofinLiveBot: disabled (stack_control owns single bot)" -ForegroundColor Yellow

# Logon task - 1 min delay so network + .venv are ready after reboot.
$BootAction = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$BootPs1`""
schtasks /Query /TN $BootTask 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    schtasks /Delete /TN $BootTask /F 2>&1 | Out-Null
}
schtasks /Create /TN $BootTask /TR $BootAction /SC ONLOGON /DELAY 0001:00 /RU $env:USERNAME /F /RL LIMITED 2>&1 | Out-Host
schtasks /Change /TN $BootTask /ENABLE 2>&1 | Out-Null
Write-Host "Scheduled: $BootTask (1 min after Windows logon)" -ForegroundColor Green

if (Test-Path $GuardInstall) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GuardInstall
} else {
    Write-Warning "install_stack_guard.ps1 not found - skip watchdog"
}

# Desktop shortcut: open dashboard + ensure stack if user boots before logon task finishes.
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "God Bot Dashboard.lnk"
try {
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut($shortcutPath)
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$OpenDash`""
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"
    $lnk.Description = "Start God Bot stack and open dashboard"
    $lnk.Save()
    Write-Host "Desktop shortcut: $shortcutPath" -ForegroundColor Green
} catch {
    Write-Warning "Could not create desktop shortcut: $_"
}

Write-Host ""
Write-Host "Running boot pass now..." -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $BootPs1

Write-Host ""
Write-Host "Done. After reboot:" -ForegroundColor Green
Write-Host '  1. Wait about 1 min after login (or double-click God Bot Dashboard on desktop)'
Write-Host '  2. Open http://127.0.0.1:5050 and press Ctrl+F5'
Write-Host '  3. Dashboard warms the bot if still starting (HF model load 60-120s)'
