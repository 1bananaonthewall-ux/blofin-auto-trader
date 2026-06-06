# Enable LLM-only live trading with local Qwen2.5-7B GGUF (sole entry brain).
# Run once:  powershell -File scripts\enable_llm_only_7b.ps1
# Then:      powershell -File scripts\stack_control.ps1 -Action restart-fresh

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== God Bot LLM-only 7B setup ===" -ForegroundColor Cyan

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\setup_local_llm.ps1") -DownloadModel 7b

$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Root ".env.example") $envFile
}

function Set-EnvLine([string]$key, [string]$value) {
    $lines = @(Get-Content $envFile -ErrorAction SilentlyContinue)
    $filtered = @($lines | Where-Object { $_ -notmatch "^$([regex]::Escape($key))=" })
    $filtered += "$key=$value"
    Set-Content -Path $envFile -Value $filtered -Encoding utf8
}

Set-EnvLine "WHATSAPP_LLM_PROVIDER" "llama_cpp"
Set-EnvLine "WHATSAPP_LLM_GGUF_PATH" "models/qwen2.5-7b-instruct-q3_k_m.gguf"
Set-EnvLine "WHATSAPP_LLM_N_GPU_LAYERS" "-1"
Set-EnvLine "LOCAL_CORTEX_ENABLED" "true"
Set-EnvLine "LLM_TRADING_ENABLED" "true"
Set-EnvLine "LLM_ONLY_TRADING" "true"
Set-EnvLine "LLM_TRADING_USE_CORTEX" "true"
Set-EnvLine "LLM_TRADING_FAIL_OPEN" "false"
Set-EnvLine "LLM_TRADING_STRICT" "true"
Set-EnvLine "LLM_TRADING_MIN_CONFIDENCE" "0.55"
Set-EnvLine "LLM_TRADING_MIN_SCORE" "48"
Set-EnvLine "LLM_POLICY_CACHE_SEC" "90"
Set-EnvLine "HOURLY_3R_WINNER_MODE" "false"
Set-EnvLine "SYMBOLS_PER_TICK" "32"
Set-EnvLine "SIGNAL_MODE" "enhanced"

Write-Host ""
Write-Host "Updated .env for LLM-only 7B GGUF trading." -ForegroundColor Green
Write-Host "Restart stack: powershell -File scripts\stack_control.ps1 -Action restart-fresh" -ForegroundColor Yellow
Write-Host "Confirm log: LLM-ONLY TRADING + CORTEX POLICY lines in logs\bot.log" -ForegroundColor Yellow
