# Ensure God Bot + dashboard are running (single bot instance, dashboard on :5050).
param([switch]$RunHourlyNow)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== God Bot Stack ===" -ForegroundColor Cyan

$ensureArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", (Join-Path $Root "scripts\stack_control.ps1"), "-Action", "ensure")
if ($RunHourlyNow) { $ensureArgs += "-RunHourlyNow" }
& powershell.exe @ensureArgs

$port = 5050
$listening = $false
try {
    $listening = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
} catch { }

if (-not $listening) {
    Write-Host "Starting dashboard on port $port..." -ForegroundColor Cyan
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File (
        Join-Path $Root "scripts\start_dashboard_quiet.ps1"
    ) -Port $port
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Quiet dashboard start failed - full restart"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (
            Join-Path $Root "scripts\run_dashboard.ps1"
        ) -Port $port
    }
    Start-Sleep -Seconds 3
    try {
        $listening = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
    } catch { $listening = $false }
    if (-not $listening) {
        Write-Host "ERROR: dashboard failed to start on port $port" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "Dashboard already listening on $port" -ForegroundColor Green
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File (Join-Path $Root "scripts\stack_control.ps1") -Action status

Write-Host ""
Write-Host "Dashboard: http://127.0.0.1:${port}" -ForegroundColor Green
Write-Host "WebSocket: ws://127.0.0.1:${port}/ws/live" -ForegroundColor Green
