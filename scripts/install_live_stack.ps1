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

# --- Stop duplicate bot.py from THIS repo only ---
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
    $cmd = $_.CommandLine
    if ($cmd -match 'bot\.py' -and $cmd -notmatch 'hourly_maintain') {
        $cwd = $_.ExecutablePath
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped old bot pid $($_.ProcessId)"
    }
}

Start-Sleep -Seconds 2
$env:PYTHONUNBUFFERED = "1"
Start-Process -FilePath $Py -ArgumentList $BotPy -WorkingDirectory $Root -WindowStyle Hidden
Write-Host "Started live bot.py (hidden window)"

# Run first hourly pass now
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $HourlyPs1
Write-Host "Done. Logs: $LogDir"
