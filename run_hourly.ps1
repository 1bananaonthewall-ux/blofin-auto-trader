# Hourly: 50x compliance, core book, 15m optimizer (live API).
Set-Location $PSScriptRoot
$LogDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "hourly_maintain.log"
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== $ts ===" | Out-File $LogFile -Append -Encoding utf8
$hourlyCmd = "`"$py`" scripts\hourly_maintain.py 2>&1"
cmd /c $hourlyCmd | Tee-Object -FilePath $LogFile -Append
