# Open Cursor Automations UI with prompt on clipboard (dashboard has no public create API).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PromptFile = Join-Path $Root ".cursor\automations\blofin-hourly.prompt.md"

if (-not (Test-Path $PromptFile)) {
    Write-Error "Missing $PromptFile"
}

$body = (Get-Content $PromptFile -Raw) -replace '(?s)^#.*?\r?\n\r?\n', ''
$body | Set-Clipboard

Write-Host @"

=== Cursor Automation (manual save — ~30 seconds) ===

Prompt is on your clipboard.

In the browser window:
  Name:     Blofin hourly optimize
  Trigger:  Schedule -> Every hour
  Repo:     1bananaonthewall-ux/blofin-auto-trader
  Branch:   main
  Tools:    Open pull request (optional), Memories (optional)
  Prompt:   Ctrl+V

"@ -ForegroundColor Cyan

Start-Process "https://cursor.com/automations/new"
Write-Host "Opened https://cursor.com/automations/new"

Write-Host @"

=== Optional: GitHub Actions hourly cloud agent ===
  1. cursor.com/dashboard -> Integrations -> User API Key
  2. GitHub repo Settings -> Secrets -> CURSOR_API_KEY
  3. Workflow: .github/workflows/blofin-hourly-cursor-cloud.yml (runs hourly)

"@ -ForegroundColor Yellow
