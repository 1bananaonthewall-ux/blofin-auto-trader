# In-session Cursor agent wake loop - emits AGENT_LOOP_TICK every 5 minutes.
# Requires Cursor IDE open with agent monitoring terminal output.
#
# Start:  powershell -File scripts\cursor_agent_5m_loop.ps1
# Stop:   kill this PowerShell process

param(
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$Prompt = @'
Run God Bot 5-minute maintenance: read .cursor/skills/blofin-5m/SKILL.md, run python scripts/agent_5m_maintain.py, check vertical curve and tph in state/agent_5m_report.json, fix anomalies (throughput, ML, TPSL, duplicate bots), brief status. Skip if last_cursor_5m.txt under 4 min old AND agent_5m_report has zero anomalies unless user messaged.
'@

$secs = [Math]::Max(60, $IntervalMinutes * 60)
Write-Host "God Bot 5m agent loop armed: every ${IntervalMinutes}m (first tick after sleep)"
Write-Host "Project: $Root"
Write-Host "Also install: scripts\install_cursor_agent_5m_loop.ps1 (Task Scheduler when IDE closed)"

while ($true) {
    Start-Sleep -Seconds $secs
    $payload = @{ prompt = $Prompt } | ConvertTo-Json -Compress
    Write-Output "AGENT_LOOP_TICK_godbot_5m $payload"
}
