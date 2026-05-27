# Blofin 3R scalper — single command: bot + winner gate + 15m optimizer + continuous ML training
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "  Blofin 3R Scalper" -ForegroundColor Cyan
Write-Host "  - Winner-only entries (BloHunter-style quality)"
Write-Host "  - 3R hard SL/TP | high leverage"
Write-Host "  - 15-minute optimizer (in-process, no separate service)"
Write-Host "  - ML trains as you go (bootstrap + live trade feedback)"
Write-Host "  Logs: logs\bot.log | state\optimizer_report.jsonl"
Write-Host ""

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtualenv..."
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
$env:PYTHONUNBUFFERED = "1"
& .\.venv\Scripts\python.exe bot.py
