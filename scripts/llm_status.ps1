# Show which local LLM the bot will use and how to upgrade speed/quality.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }

Write-Host "`n=== God Bot local LLM ===" -ForegroundColor Cyan
& $py -c @"
from config import load_settings
load_settings()
from local_llm import resolve_provider, status_line, gguf_path, openai_compat_healthy, discover_openai_model, _base_url
p = resolve_provider()
print('active_provider:', p)
print('status:', status_line())
g = gguf_path()
print('gguf:', g.name if g else '(none — run setup_local_llm.ps1 -DownloadModel 3b)')
lm = openai_compat_healthy()
print('lm_studio:', 'up @ ' + _base_url() if lm else 'down (start LM Studio, load Qwen2.5-7B-Instruct, port 1234)')
if lm:
    print('lm_model:', discover_openai_model())
"@

Write-Host "`nTier guide:" -ForegroundColor Yellow
Write-Host "  1) LM Studio + 7B (smartest, fast after load) — WHATSAPP_LLM_PROVIDER=auto"
Write-Host "  2) GGUF 3B in-process (fast+smart) — .\scripts\setup_local_llm.ps1 -DownloadModel 3b"
Write-Host "  3) HF 1.5B fallback (no download) — slower on CPU; upgrade to 3b/7b when you can"
