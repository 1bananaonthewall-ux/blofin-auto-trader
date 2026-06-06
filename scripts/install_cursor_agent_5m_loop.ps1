# Register BlofinCursorAgent5m - every 5 min automated maintain + AGENT_5M_DUE flag.
# Complements BlofinStackGuard (infra) and BlofinHourlyMaintain (deep hourly pass).
#
# Run once:  powershell -File scripts\install_cursor_agent_5m_loop.ps1
# In-session wake (IDE open): powershell -File scripts\cursor_agent_5m_loop.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$TickPs1 = Join-Path $Root "scripts\agent_5m_tick.ps1"
$LoopPs1 = Join-Path $Root "scripts\cursor_agent_5m_loop.ps1"

if (-not (Test-Path $TickPs1)) { Write-Error "Missing $TickPs1" }

$tickAction = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$TickPs1`""

Write-Host "=== Installing God Bot 5-minute Cursor agent tick ===" -ForegroundColor Cyan

schtasks /Create /F /TN "BlofinCursorAgent5m" /SC MINUTE /MO 5 /TR $tickAction /RL LIMITED 2>&1 | Out-Host
schtasks /Change /TN "BlofinCursorAgent5m" /ENABLE 2>&1 | Out-Host
Write-Host "Registered BlofinCursorAgent5m (every 5 min)" -ForegroundColor Green

Write-Host ""
Write-Host "Running first 5m tick now..." -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $TickPs1

Write-Host ""
Write-Host @"

5-minute agent maintenance installed.

  Scheduled (always):  BlofinCursorAgent5m -> agent_5m_maintain.py + .cursor/AGENT_5M_DUE
  IDE wake loop:       powershell -File scripts\cursor_agent_5m_loop.ps1
  Skill:               .cursor/skills/blofin-5m/SKILL.md

When Cursor is open, start the IDE loop so the agent wakes on AGENT_LOOP_TICK_godbot_5m.
When IDE is closed, the scheduled tick still runs optimizer/throughput/ML hooks and sets AGENT_5M_DUE
for the next session.

"@ -ForegroundColor Yellow

Write-Host "Optional - start IDE loop now in a hidden window:" -ForegroundColor Cyan
Write-Host ('  Start-Process powershell -WindowStyle Hidden -ArgumentList ''-NoProfile'',''-ExecutionPolicy'',''Bypass'',''-File'',''' + $LoopPs1 + '''')
