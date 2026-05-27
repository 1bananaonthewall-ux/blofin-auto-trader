# Local hourly Blofin check (uses your .env). Run from Cursor terminal or Task Scheduler.
Set-Location $PSScriptRoot
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $py scripts\hourly_maintain.py
