# LLM-only trading with local Qwen2.5-1.5B-Instruct (HF transformers — already cached).
# Run:  powershell -File scripts\enable_llm_only_1.5b.ps1
# Then: powershell -File scripts\stack_control.ps1 -Action restart-fresh

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== God Bot LLM-only 1.5B (hf_local) ===" -ForegroundColor Cyan

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

Set-EnvLine "WHATSAPP_LLM_PROVIDER" "hf_local"
Set-EnvLine "WHATSAPP_LLM_HF_MODEL" "Qwen/Qwen2.5-1.5B-Instruct"
Set-EnvLine "WHATSAPP_LLM_HF_LOCAL_ONLY" "true"
Set-EnvLine "WHATSAPP_LLM_FAST" "true"
Set-EnvLine "WHATSAPP_LLM_SKIP_LLAMA" "true"
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
Set-EnvLine "WINNER_ONLY_MODE" "false"
Set-EnvLine "ML_CONTINUOUS_TRAIN" "false"
Set-EnvLine "ML_AUTO_REFIT_ON_STARTUP" "false"
Set-EnvLine "MARKOV_REGIME_ENABLED" "false"
Set-EnvLine "SYMBOL_QUALITY_ENABLED" "false"
Set-EnvLine "RUNNER_FILTER_ENABLED" "false"
Set-EnvLine "TRADE_LESSONS_ENABLED" "false"
Set-EnvLine "HOURLY_BRAIN_ENABLED" "false"
Set-EnvLine "MOON_SWARM_ENABLED" "false"
Set-EnvLine "SYMBOLS_PER_TICK" "32"
Set-EnvLine "SIGNAL_MODE" "enhanced"

Write-Host "Updated .env for LLM-only Qwen2.5-1.5B hf_local." -ForegroundColor Green
Write-Host "Restart: powershell -File scripts\stack_control.ps1 -Action restart-fresh" -ForegroundColor Yellow
