# Five-minute tick: automated maintain + AGENT_5M_DUE flag for Cursor agent.
# Registered as BlofinCursorAgent5m (every 5 min) or run manually.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "cursor_agent_5m.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$ts tick start" | Out-File $Log -Append -Encoding utf8

$Py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $Py scripts\agent_5m_maintain.py 2>&1 | ForEach-Object { "$_" | Out-File $Log -Append -Encoding utf8 }
"$ts tick done exit=$LASTEXITCODE" | Out-File $Log -Append -Encoding utf8
