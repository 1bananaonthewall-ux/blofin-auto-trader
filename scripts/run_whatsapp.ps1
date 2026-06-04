# Start WhatsApp webhook (Twilio -> Flask). Requires ngrok on port 5000.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }

if (-not (Test-Path ".\models\*.gguf")) {
    Write-Warning "No GGUF in .\models\ — run: .\scripts\setup_local_llm.ps1 -DownloadModel 7b"
}
if (Test-Path ".\.venv\Scripts\pip.exe") {
    & .\.venv\Scripts\pip.exe show llama-cpp-python 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & $PSScriptRoot\setup_local_llm.ps1
    }
}

Write-Host "LLM status:"
& $Py -c "from local_llm import status_line; print(status_line())"

Write-Host ""
Write-Host "Starting whatsapp_bot on http://0.0.0.0:5000"
Write-Host "Point Twilio webhook to: https://YOUR_NGROK_URL/whatsapp"
& $Py whatsapp_bot.py
