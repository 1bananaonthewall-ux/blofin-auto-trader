# Full mission stack: install tasks, start bot, run health + hourly pass.
Set-Location $PSScriptRoot
Write-Host "=== Blofin Mission Stack ===" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\scripts\install_live_stack.ps1"
& powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\scripts\install_stack_guard.ps1"
Start-Sleep -Seconds 8
& .\.venv\Scripts\python.exe scripts\stack_status.py
& .\.venv\Scripts\python.exe scripts\hourly_maintain.py
Write-Host "`nStack is live. Logs: logs\bot.log logs\stack_guard.log logs\hourly_maintain.log" -ForegroundColor Green
