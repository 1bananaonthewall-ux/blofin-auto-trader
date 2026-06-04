# Fast + robust local LLM: LM Studio OR 3B GGUF (recommended on Windows)
param(
    [switch]$SkipDownload,
    [switch]$CpuOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host ""
Write-Host "=== Fast smart LLM setup ===" -ForegroundColor Cyan
Write-Host "Tier 1 (fastest): LM Studio on port 1234 with Qwen2.5 loaded."
Write-Host "Tier 2: In-process 3B GGUF (~2GB download)."
Write-Host ""

$lmOk = $false
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:1234/v1/models" -TimeoutSec 3 -UseBasicParsing
    $lmOk = ($r.StatusCode -eq 200)
} catch {
    $lmOk = $false
}

if ($lmOk) {
    Write-Host "LM Studio is UP - auto provider will use it." -ForegroundColor Green
}
elseif (-not $SkipDownload) {
    $localArgs = @("-DownloadModel", "3b")
    if ($CpuOnly) { $localArgs += "-CpuOnly" }
    & "$PSScriptRoot\setup_local_llm.ps1" @localArgs
}
else {
    Write-Host "No LM Studio. Run: .\scripts\setup_local_llm.ps1 -DownloadModel 3b" -ForegroundColor Yellow
}

$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    function Set-Line([string]$k, [string]$v) {
        $lines = @(Get-Content $envFile | Where-Object { $_ -notmatch "^$([regex]::Escape($k))=" })
        $lines += "$k=$v"
        Set-Content -Path $envFile -Value $lines -Encoding utf8
    }
    Set-Line "WHATSAPP_LLM_PROVIDER" "auto"
    Set-Line "WHATSAPP_LLM_GGUF_PATH" "models/qwen2.5-3b-instruct-q4_k_m.gguf"
    Set-Line "WHATSAPP_LLM_FAST" "true"
    Set-Line "WHATSAPP_LLM_N_GPU_LAYERS" $(if ($CpuOnly) { "0" } else { "-1" })
    Set-Line "LLM_TRADING_ENABLED" "true"
    Set-Line "LLM_TRADING_USE_CORTEX" "true"
    Set-Line "LLM_TRADING_MAX_TOKENS" "128"
    Set-Line "LLM_POLICY_CACHE_SEC" "45"
    Set-Line "LOCAL_CORTEX_POLICY_MAX_KNOWLEDGE_CHARS" "1400"
    Write-Host "Updated .env for fast LLM + trading brain." -ForegroundColor Green
}

$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $py -c "from config import load_settings; load_settings(); from local_llm import resolve_provider, status_line, warmup_provider; print('provider:', resolve_provider()); print(warmup_provider())"

Write-Host ""
Write-Host "Restart bot + dashboard after setup." -ForegroundColor Cyan
