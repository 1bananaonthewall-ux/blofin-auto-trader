# Option A: llama-cpp-python + GGUF in ./models (no Ollama)
param(
    [ValidateSet("3b", "7b", "14b")]
    [string]$DownloadModel = "",
    [switch]$CpuOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
$Pip = if (Test-Path ".\.venv\Scripts\pip.exe") { ".\.venv\Scripts\pip.exe" } else { "pip" }

New-Item -ItemType Directory -Force -Path ".\models" | Out-Null

Write-Host "Installing WhatsApp deps..."
& $Pip install -q flask twilio

$wheelArgs = @("install", "-q", "llama-cpp-python", "--only-binary=:all:")
if (-not $CpuOnly -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Host "NVIDIA detected - CUDA wheel cu124..."
    & $Pip @wheelArgs --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
} else {
    Write-Host "CPU wheel..."
    & $Pip @wheelArgs --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
}
if ($LASTEXITCODE -ne 0) {
    Write-Warning "llama-cpp-python install failed. Enable Windows long paths, reboot, re-run."
}

if ($DownloadModel) {
    & $Py scripts\download_gguf.py --model $DownloadModel
}

function Ensure-EnvLine([string]$path, [string]$key, [string]$value) {
    if (-not (Test-Path $path)) { return }
    $lines = Get-Content $path -ErrorAction SilentlyContinue
    $filtered = @($lines | Where-Object { $_ -notmatch "^$([regex]::Escape($key))=" })
    $filtered += "$key=$value"
    Set-Content -Path $path -Value $filtered -Encoding utf8
}

$envFile = Join-Path $Root ".env"
$ggufName = switch ($DownloadModel) {
    "14b" { "qwen2.5-14b-instruct-q3_k_m.gguf" }
    "7b"  { "qwen2.5-7b-instruct-q3_k_m.gguf" }
    "3b"  { "qwen2.5-3b-instruct-q4_k_m.gguf" }
    default { "qwen2.5-7b-instruct-q3_k_m.gguf" }
}
if (Test-Path $envFile) {
    Ensure-EnvLine $envFile "WHATSAPP_LLM_PROVIDER" "llama_cpp"
    Ensure-EnvLine $envFile "WHATSAPP_LLM_GGUF_PATH" "models/$ggufName"
    Ensure-EnvLine $envFile "WHATSAPP_LLM_N_CTX" "8192"
    Ensure-EnvLine $envFile "WHATSAPP_LLM_N_GPU_LAYERS" $(if ($CpuOnly) { "0" } else { "-1" })
    Ensure-EnvLine $envFile "LOCAL_CORTEX_ENABLED" "true"
    Write-Host "Updated .env for llama_cpp."
}

Write-Host ""
& $Py scripts\train_local_cortex.py 2>$null | Out-Null
& $Py scripts\cortex_status.py 2>$null

if (-not $DownloadModel -and -not (Get-ChildItem ".\models\*.gguf" -ErrorAction SilentlyContinue)) {
    Write-Host "Next (fast+smart): .\scripts\setup_local_llm.ps1 -DownloadModel 3b"
    Write-Host "     (best quality): .\scripts\setup_local_llm.ps1 -DownloadModel 7b"
}
