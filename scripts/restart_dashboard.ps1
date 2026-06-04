# Kill stale dashboard_api, rebuild UI, start fresh on :5050
param([int]$Port = 5050)
& (Join-Path $PSScriptRoot "run_dashboard.ps1") -Port $Port
