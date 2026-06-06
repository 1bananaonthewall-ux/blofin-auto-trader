# LLM overseer mode: 1.5B supervises ML swarm, optimizes every 5 minutes.
# Run:  powershell -File scripts\enable_llm_overseer_1.5b.ps1
# Then: powershell -File scripts\stack_control.ps1 -Action restart-fresh

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== God Bot LLM Overseer (1.5B + ML swarm) ===" -ForegroundColor Cyan

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

Set-EnvLine "LLM_OVERSEER_MODE" "true"
Set-EnvLine "OVERSEER_INTERVAL_SECONDS" "300"
Set-EnvLine "LLM_ONLY_TRADING" "false"
Set-EnvLine "LLM_TRADING_ENABLED" "false"
Set-EnvLine "WHATSAPP_LLM_PROVIDER" "hf_local"
Set-EnvLine "WHATSAPP_LLM_HF_MODEL" "Qwen/Qwen2.5-1.5B-Instruct"
Set-EnvLine "WHATSAPP_LLM_HF_LOCAL_ONLY" "true"
Set-EnvLine "WHATSAPP_LLM_FAST" "true"
Set-EnvLine "WHATSAPP_LLM_SKIP_LLAMA" "true"
Set-EnvLine "LOCAL_CORTEX_ENABLED" "true"
Set-EnvLine "SIGNAL_MODE" "ml"
Set-EnvLine "ML_CONTINUOUS_TRAIN" "true"
Set-EnvLine "ML_AUTO_REFIT_ON_STARTUP" "true"
Set-EnvLine "ML_WALK_FORWARD_SPLITS" "10"
Set-EnvLine "ML_WALK_FORWARD_MIN_TRAIN" "300"
Set-EnvLine "ML_OUTCOME_REFIT_MIN_NEW" "2"
Set-EnvLine "ML_USE_TRIPLE_BARRIER" "true"
Set-EnvLine "ML_REAL_FEEDBACK_MAX_SAMPLES" "1000"
Set-EnvLine "USE_ENHANCED_STRATEGY" "true"
Set-EnvLine "TRADE_LESSONS_ENABLED" "true"
Set-EnvLine "HOURLY_3R_WINNER_MODE" "true"
Set-EnvLine "WINNER_ONLY_MODE" "true"
Set-EnvLine "MOON_SWARM_ENABLED" "true"
Set-EnvLine "MARKOV_REGIME_ENABLED" "true"
Set-EnvLine "SYMBOL_QUALITY_ENABLED" "true"
Set-EnvLine "OPTIMIZER_AUTOCODE_ENABLED" "true"
Set-EnvLine "OPTIMIZER_TARGET_MIN_TPH" "4"
Set-EnvLine "OPTIMIZER_TARGET_MIN_WINS_PER_HOUR" "3"
Set-EnvLine "SYMBOLS_PER_TICK" "120"
Set-EnvLine "ENTRIES_PAUSED" "false"

Write-Host "Updated .env for LLM overseer (5m optimize cycle)." -ForegroundColor Green
Write-Host "Restart: powershell -File scripts\stack_control.ps1 -Action restart-fresh" -ForegroundColor Yellow
Write-Host "Log line: OVERSEER 5m optimize" -ForegroundColor Yellow
