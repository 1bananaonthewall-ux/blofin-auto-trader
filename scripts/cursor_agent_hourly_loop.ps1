# Background wake loop for Cursor agent hourly God Bot checks (IDE session must stay open).
# Emits AGENT_LOOP_TICK lines the agent monitors via notify_on_output.
#
# Stop: kill the PowerShell job or close its window.
# Prefer install_hourly_loop.ps1 for maintenance when Cursor is closed.

param(
    [int]$IntervalMinutes = 60
)

$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$Prompt = @'
Run blofin-hourly maintenance for God Bot: read .cursor/skills/blofin-hourly/SKILL.md, ensure exactly one bot.py via stack_control ensure, check logs/bot.log tail for errors and ML forward refit, run optimizer if needed, brief status summary. Skip if last_cursor_hourly.txt is under 50 minutes old unless anomalies in logs.
'@

$secs = [Math]::Max(60, $IntervalMinutes * 60)
Write-Host "God Bot agent loop armed: every ${IntervalMinutes}m (first tick after sleep)"
Write-Host "Project: $Root"

while ($true) {
    Start-Sleep -Seconds $secs
    $payload = @{ prompt = $Prompt } | ConvertTo-Json -Compress
    Write-Output "AGENT_LOOP_TICK_godbot $payload"
}
