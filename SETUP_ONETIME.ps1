# One-time: GitHub login → push private repo → open Cursor automation builder.
# Run in PowerShell:  .\SETUP_ONETIME.ps1

$Root = $PSScriptRoot
$Gh = "C:\Program Files\GitHub CLI\gh.exe"
if (-not (Test-Path $Gh)) { $Gh = "gh" }

Write-Host "`n=== Step 1/3: GitHub login (browser will open) ===" -ForegroundColor Cyan
& $Gh auth login -h github.com -p https -s repo -w
if ($LASTEXITCODE -ne 0) { Write-Error "GitHub login failed"; exit 1 }

Write-Host "`n=== Step 2/3: Push private repo ===" -ForegroundColor Cyan
Set-Location $Root
& powershell -NoProfile -ExecutionPolicy Bypass -File "$Root\scripts\publish_github.ps1"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n=== Step 3/3: Cursor automation (browser) ===" -ForegroundColor Cyan
$prompt = Get-Content "$Root\.cursor\automations\blofin-hourly.prompt.md" -Raw
$prompt | Set-Clipboard
Write-Host "Prompt copied to clipboard. Paste into Cursor Automations."
Write-Host @"

In the browser:
  1. Schedule: Every hour
  2. Repository: blofin-auto-trader / main
  3. Paste prompt (Ctrl+V)
  4. Enable Memories (optional)
  5. Save automation

"@ -ForegroundColor Yellow
Start-Process "https://cursor.com/automations/new"
Write-Host "Done.`n"
