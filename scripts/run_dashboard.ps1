# God Bot Dashboard — API + optional UI dev server
param(
    [switch]$Dev,
    [int]$Port = 5050
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }

function Stop-DashboardApi {
    param([int]$ListenPort)

    $pids = @(
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object {
                $cmd = $_.CommandLine
                $cmd -and $cmd -match "dashboard_api\.py" -and $cmd -notmatch '\\\.venv\\Scripts\\python\.exe"'
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
        Write-Host "Stopped dashboard pid $procId" -ForegroundColor Yellow
    }

    if ($pids.Count -gt 0) {
        Start-Sleep -Seconds 1
    }
}

Stop-DashboardApi -ListenPort $Port

$dashDir = Join-Path $Root "dashboard"
if (Test-Path (Join-Path $dashDir "package.json")) {
    Write-Host "Building dashboard UI..." -ForegroundColor Cyan
    Push-Location $dashDir
    npm run build 2>&1 | Out-Host
    Pop-Location
}

Write-Host "Starting God Bot Dashboard API on port $Port" -ForegroundColor Cyan
$env:DASHBOARD_PORT = "$Port"

if ($Dev) {
    Start-Process -FilePath $py -ArgumentList "dashboard_api.py" -WorkingDirectory $Root -WindowStyle Normal
    Start-Sleep -Seconds 2
    Push-Location $dashDir
    npm run dev
    Pop-Location
} else {
    Start-Process -FilePath $py -ArgumentList "dashboard_api.py" -WorkingDirectory $Root -WindowStyle Hidden
    Start-Sleep -Seconds 2
    Write-Host "Dashboard ready: http://127.0.0.1:$Port (hard refresh if tab was open)" -ForegroundColor Green
}
