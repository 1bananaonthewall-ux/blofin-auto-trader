# Train cortex, ensure GGUF model, smoke-test LLM, start WhatsApp webhook
param(
    [ValidateSet("7b", "14b")]
    [string]$Model = "7b",
    [switch]$SkipDownload,
    [switch]$SkipWhatsApp
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }

Write-Host "=== 1/4 Train cortex from live outcomes ==="
& $Py scripts\train_local_cortex.py

Write-Host "`n=== 2/4 Local LLM runtime ==="
if ($SkipDownload) {
    & $PSScriptRoot\setup_local_llm.ps1
} else {
    & $PSScriptRoot\setup_local_llm.ps1 -DownloadModel $Model
}

$gguf = Get-ChildItem ".\models\*.gguf" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $gguf) {
    Write-Error "No GGUF in models/. Run without -SkipDownload or place a .gguf file."
}

Write-Host "`n=== 3/4 Smoke test (cortex + llama_cpp) ==="
& $Py scripts\cortex_smoke_test.py
if ($LASTEXITCODE -ne 0) { Write-Warning "Smoke test failed — model may still be downloading." }

if ($SkipWhatsApp) {
    Write-Host "`n=== 4/4 Skipped WhatsApp (-SkipWhatsApp) ==="
    exit 0
}

Write-Host "`n=== 4/4 Start WhatsApp (background) ==="
$log = Join-Path $Root "logs\whatsapp.log"
Start-Process -FilePath $Py -ArgumentList "whatsapp_bot.py" -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $log
Start-Sleep -Seconds 2
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:5000/health" -UseBasicParsing -TimeoutSec 5
    Write-Host "WhatsApp health:" $r.Content
} catch {
    Write-Warning "WhatsApp not responding yet — check logs\whatsapp.log (Twilio/ngrok still required for inbound)."
}
Write-Host "Done. Cortex trained; local LLM ready; WhatsApp process started."
