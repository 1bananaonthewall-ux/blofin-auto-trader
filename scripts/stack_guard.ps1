# Watchdog: keep God Bot + dashboard running (run every 5 min via Task Scheduler).
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "stack_guard.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$ts guard tick" | Out-File $Log -Append -Encoding utf8

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stack_control.ps1") -Action ensure 2>&1 |
    Out-File $Log -Append -Encoding utf8

$port = 5050
$listening = $false
try {
    $listening = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
} catch { }

if (-not $listening) {
    "$ts RESTART dashboard :$port" | Out-File $Log -Append -Encoding utf8
    Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $Root "scripts\run_dashboard.ps1"),
        "-Port", "$port"
    )
}

$Py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $Py scripts\log_watch_optimizer.py 2>&1 | Out-File $Log -Append -Encoding utf8
& $Py scripts\repair_open_tpsl.py 2>&1 | Out-File $Log -Append -Encoding utf8
& $Py scripts\stack_status.py 2>&1 | Out-File $Log -Append -Encoding utf8
