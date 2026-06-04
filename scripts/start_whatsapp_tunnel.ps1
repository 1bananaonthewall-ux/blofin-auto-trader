# Step 4 helper: expose whatsapp_bot (port 5000) and print Twilio webhook URL.
param(
    [int]$Port = 5000
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

try {
    $h = Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3
    Write-Host "OK bot health:" $h.Content
} catch {
    Write-Host "Starting whatsapp_bot.py..."
    $py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
    Start-Process -FilePath $py -ArgumentList "whatsapp_bot.py" -WorkingDirectory $Root -WindowStyle Hidden
    Start-Sleep 3
}

Write-Host ""
Write-Host "Starting ngrok on port $Port (leave this window open)..."
Write-Host "After ngrok starts, open http://127.0.0.1:4040 and copy the https URL."
Write-Host "Twilio webhook = https://YOUR-URL/whatsapp"
Write-Host ""
Write-Host "If ngrok says authtoken invalid, run once:"
Write-Host "  ngrok config add-authtoken YOUR_TOKEN"
Write-Host "  Get token: https://dashboard.ngrok.com/get-started/your-authtoken"
Write-Host ""

ngrok http $Port
