# Start dashboard API only if port is not already listening (no kill/rebuild).
param([int]$Port = 5050)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$listening = $false
try {
    $listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
} catch { }

if ($listening) {
    exit 0
}

# Kill stray system-python dashboard duplicates before starting venv worker.
@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match "dashboard_api\.py" -and $_.CommandLine -notlike "*\.venv\Scripts\python.exe*"
}) | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }
$distIndex = Join-Path $Root "dashboard\dist\index.html"

if (-not (Test-Path $distIndex)) {
    $dashDir = Join-Path $Root "dashboard"
    if (Test-Path (Join-Path $dashDir "package.json")) {
        Push-Location $dashDir
        npm run build 2>&1 | Out-Null
        Pop-Location
    }
}

$env:DASHBOARD_PORT = "$Port"
$env:PYTHONUNBUFFERED = "1"
Start-Process -FilePath $py -ArgumentList "dashboard_api.py" -WorkingDirectory $Root -WindowStyle Hidden | Out-Null

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        if (@(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
            exit 0
        }
    } catch { }
}
exit 1
