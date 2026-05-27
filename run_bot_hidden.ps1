# Hidden live bot — used by Task Scheduler (BlofinLiveBot).
Set-Location $PSScriptRoot
$env:PYTHONUNBUFFERED = "1"
$LogDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $py bot.py 2>&1 | ForEach-Object {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $_" | Out-File (Join-Path $LogDir "bot_scheduled.log") -Append -Encoding utf8
}
