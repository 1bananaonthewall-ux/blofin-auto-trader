# One-shot: hourly maintenance task + ensure single bot from this repo (LIVE).
# Run as your Windows user (no admin required for schtasks /RU %USERNAME%).

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = if (Test-Path ".\.venv\Scripts\python.exe") { Resolve-Path ".\.venv\Scripts\python.exe" } else { (Get-Command python).Source }
$HourlyPs1 = Join-Path $Root "run_hourly.ps1"
$BotPy = Join-Path $Root "bot.py"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$skipStart = $false
Write-Host "Blofin live stack installer"
Write-Host "  Root: $Root"
Write-Host "  Python: $Py"

# --- Hourly: health + optimizer + non-50x closes ---
$HourlyTask = "BlofinHourlyMaintain"
$null = schtasks /Query /TN $HourlyTask 2>&1
if ($LASTEXITCODE -eq 0) { schtasks /Delete /TN $HourlyTask /F 2>&1 | Out-Null }
$HourlyAction = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$HourlyPs1`""
schtasks /Create /TN $HourlyTask /TR $HourlyAction /SC HOURLY /MO 1 /RU $env:USERNAME /F /RL LIMITED | Out-Null
Write-Host "Scheduled: $HourlyTask (every hour)"

# --- Bot at logon (single runner) ---
$BotTask = "BlofinLiveBot"
$null = schtasks /Query /TN $BotTask 2>&1
if ($LASTEXITCODE -eq 0) { schtasks /Delete /TN $BotTask /F 2>&1 | Out-Null }
$BotHidden = Join-Path $Root "run_bot_hidden.ps1"
$BotAction = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$BotHidden`""
$r = schtasks /Create /TN $BotTask /TR $BotAction /SC ONLOGON /RU $env:USERNAME /F /RL LIMITED 2>&1
if ($LASTEXITCODE -ne 0) { Write-Warning "Bot logon task: $r" } else { Write-Host "Scheduled: $BotTask (at logon)" }

# --- Single bot.py from this repo (.venv preferred) ---
$bots = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like "*$Root*bot.py*" -and $_.CommandLine -notmatch "hourly_maintain"
})
if ($bots.Count -gt 1) {
    $keep = ($bots | Where-Object { $_.CommandLine -like "*.venv*" } | Select-Object -First 1)
    if (-not $keep) { $keep = $bots[0] }
    foreach ($b in $bots) {
        if ($b.ProcessId -ne $keep.ProcessId) {
            Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped duplicate bot pid $($b.ProcessId)"
        }
    }
} elseif ($bots.Count -eq 1) {
    Write-Host "Bot already running pid $($bots[0].ProcessId)"
    $skipStart = $true
}

if (-not $skipStart) {
    Start-Sleep -Seconds 2
    $env:PYTHONUNBUFFERED = "1"
    Start-Process -FilePath $Py -ArgumentList $BotPy -WorkingDirectory $Root -WindowStyle Hidden
    Write-Host "Started live bot.py (hidden window)"
}

# Run first hourly pass now
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $HourlyPs1
Write-Host "Done. Logs: $LogDir"
