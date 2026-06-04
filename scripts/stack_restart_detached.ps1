# Launched detached from dashboard_api — must survive dashboard process exit.
param([int]$DashboardPort = 5050)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "stack_restart.log"

function Write-Log([string]$Msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $Msg" | Out-File $Log -Append -Encoding utf8
}

Write-Log "=== stack restart (dashboard button) port=$DashboardPort ==="

$control = Join-Path $Root "scripts\stack_control.ps1"
$dashPs1 = Join-Path $Root "scripts\run_dashboard.ps1"

try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $control -Action restart-fresh -DashboardPort $DashboardPort 2>&1 |
        ForEach-Object { Write-Log $_.ToString() }
    Write-Log "restart-fresh finished exit=$LASTEXITCODE"
} catch {
    Write-Log "restart-fresh exception: $_"
}

Start-Sleep -Seconds 2

try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $control -Action ensure 2>&1 |
        ForEach-Object { Write-Log "ensure: $($_.ToString())" }
} catch {
    Write-Log "ensure exception: $_"
}

$listening = $false
try {
    $listening = @(Get-NetTCPConnection -LocalPort $DashboardPort -State Listen -ErrorAction SilentlyContinue).Count -gt 0
} catch { }

if (-not $listening) {
    Write-Log "dashboard not listening on $DashboardPort — starting run_dashboard.ps1"
    Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $dashPs1,
        "-Port", "$DashboardPort"
    ) | Out-Null
    Start-Sleep -Seconds 8
}

$bots = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine -match "bot\.py" -and $_.CommandLine -like "*$Root*" -and
    $_.CommandLine -notmatch "hourly_maintain|dashboard_api"
})
if ($bots.Count -eq 0) {
    Write-Log "WARN: no bot.py detected after restart"
} else {
    Write-Log "bot ok pid=$($bots[0].ProcessId)"
}

Write-Log "=== stack restart done ==="
