# Endless wallet consolidation → $100 USDT sweeps to Blofin
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    .\.venv\Scripts\pip install -r requirements.txt
}

if (-not (Test-Path "treasury\wallets.json")) {
    Copy-Item "treasury\wallets.example.json" "treasury\wallets.json"
    Write-Host "Created treasury\wallets.json — edit it with your wallet addresses."
}

.\.venv\Scripts\python treasury_loop.py
