# Run walk-forward backtest WITHOUT stopping live God Bot (low CPU priority, detached).
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
$script = Join-Path $Root "scripts\god_backtest_live_optimize.py"
$log = Join-Path $Root "logs\god_backtest_sideload.log"

# Confirm live bot before starting research job.
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stack_control.ps1") -Action status |
    Out-File $log -Append -Encoding utf8

$args = @(
    $script,
    "--rounds", "2",
    "--max-assets", "80",
    "--lookback-days", "365",
    "--train-days", "90",
    "--test-days", "21",
    "--workers", "4"
)
if ($env:GOD_BACKTEST_REFIT_ML -eq "1") { $args += "--refit-ml" }

Start-Process -FilePath $py -ArgumentList $args -WorkingDirectory $Root -WindowStyle Hidden -PriorityClass BelowNormal |
    Out-Null

Write-Host "Backtest sideload started (BelowNormal priority). Log: $log"
Write-Host "Live bot untouched. Progress: state\god_backtest\live_progress.json"
Write-Host "Trading log: logs\bot.log"
