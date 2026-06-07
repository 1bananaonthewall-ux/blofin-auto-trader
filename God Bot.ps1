param(
    [ValidateSet("start", "stop", "restart", "status", "ensure")]
    [string]$Action = "ensure",
    [switch]$RunHourlyNow
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "God Bot control -> action: $Action"

if ($Action -in @("start", "restart", "ensure")) {
    $stackArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $Root "scripts\run_god_bot_stack.ps1")
    )
    if ($RunHourlyNow) {
        $stackArgs += "-RunHourlyNow"
    }
    powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden @stackArgs
} else {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ".\scripts\stack_control.ps1" -Action $Action
}

if ($Action -in @("start", "restart", "ensure")) {
    Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $Root "scripts\open_log_window.ps1")
    )
    Write-Host "Live log window opened (maximized)."
}
