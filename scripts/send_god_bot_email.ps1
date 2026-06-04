# Email God Bot.zip — SMTP (.env) or open a ready-to-send .eml draft.
param(
    [string]$To = "abcdiscjockey@gmail.com",
    [string]$ZipPath = "$env:USERPROFILE\Downloads\God Bot.zip"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = if (Test-Path "$Root\.venv\Scripts\python.exe") { "$Root\.venv\Scripts\python.exe" } else { "python" }

if (-not (Test-Path $ZipPath)) {
    Write-Host "Missing zip: $ZipPath - rebuild with packaging script or re-run agent pack step." -ForegroundColor Red
    exit 1
}

& $Py "$Root\scripts\send_god_bot_email.py" --to $To --zip $ZipPath
if ($LASTEXITCODE -eq 0) {
    Write-Host "Email sent via SMTP." -ForegroundColor Green
    exit 0
}

Write-Host "SMTP not configured - creating .eml draft..." -ForegroundColor Yellow
$eml = Join-Path (Split-Path $ZipPath) "God Bot - email draft.eml"
$boundary = "----=_GodBot_" + [guid]::NewGuid().ToString("N")
$zipBytes = [IO.File]::ReadAllBytes($ZipPath)
$b64 = [Convert]::ToBase64String($zipBytes, [Base64FormattingOptions]::InsertLineBreaks)
$bodyText = @"
Hi,

Attached is God Bot.zip (Blofin auto-trader for a new Windows PC).

In the zip:
- AGENT_READ_ME_FIRST.md - give this to Cursor on the new machine
- SETUP_NEW_COMPUTER.md - install steps
- Full code (no API keys; use .env.example)

Unzip, pip install -r requirements.txt, configure .env, then:
  powershell -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action ensure
"@

$from = $env:SMTP_USER
if (-not $from) { $from = "godbot@localhost" }

$emlContent = @"
From: $from
To: $To
Subject: God Bot - portable package + agent manual
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="$boundary"

--$boundary
Content-Type: text/plain; charset=utf-8

$bodyText

--$boundary
Content-Type: application/zip; name="God Bot.zip"
Content-Transfer-Encoding: base64
Content-Disposition: attachment; filename="God Bot.zip"

$b64

--$boundary--
"@

Set-Content -Path $eml -Value $emlContent -Encoding UTF8
Write-Host "Draft: $eml" -ForegroundColor Cyan
Start-Process $eml
Write-Host @"

If Mail opens: choose your Gmail account, confirm To=$To, click Send.

To send automatically next time, add to .env:
  SMTP_USER=your@gmail.com
  SMTP_APP_PASSWORD=(Gmail app password, 16 chars)
  SMTP_FROM=your@gmail.com

Then: .\scripts\send_god_bot_email.ps1

"@ -ForegroundColor Yellow
exit 0
