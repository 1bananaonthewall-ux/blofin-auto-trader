# First-time God Bot setup after git clone (venv, deps, .env template, dashboard).
param(
    [switch]$SkipDashboard,
    [switch]$SkipEnvCopy
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== God Bot bootstrap ===" -ForegroundColor Cyan

function Require-Command($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "Missing command: $name. Install Python 3.12+ from https://www.python.org/downloads/"
    }
}

Require-Command python
$pyVer = (python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
Write-Host "Python $pyVer"

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating .venv ..."
    python -m venv .venv
}
$pip = Join-Path $Root ".venv\Scripts\pip.exe"
& $pip install --upgrade pip wheel
& $pip install -r requirements.txt
& $pip install -r requirements-dashboard.txt

$envPath = Join-Path $Root ".env"
$example = Join-Path $Root ".env.example"
if (-not $SkipEnvCopy) {
    if (-not (Test-Path $envPath) -and (Test-Path $example)) {
        Copy-Item $example $envPath
        Write-Host "Created .env from .env.example - EDIT with YOUR Blofin API keys." -ForegroundColor Yellow
    } elseif (Test-Path $envPath) {
        Write-Host ".env already exists - not overwritten." -ForegroundColor Green
    }
}

New-Item -ItemType Directory -Path (Join-Path $Root "state") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Root "logs") -Force | Out-Null

if (-not $SkipDashboard) {
    $distIndex = Join-Path $Root "dashboard\dist\index.html"
    if (Test-Path $distIndex) {
        Write-Host "Dashboard dist present - skip build." -ForegroundColor Green
    } elseif (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "Building dashboard ..."
        Push-Location (Join-Path $Root "dashboard")
        npm install --no-fund --no-audit
        npm run build
        Pop-Location
    } else {
        Write-Host "Node/npm not found - install Node 20+ or use prebuilt dashboard/dist from repo." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. notepad .env   # BLOFIN_API_KEY, BLOFIN_SECRET, BLOFIN_PASSPHRASE"
Write-Host "  2. BLOFIN_MODE=demo until ready for live"
Write-Host "  3. .\.venv\Scripts\python.exe smoke_test.py"
Write-Host "  4. powershell -ExecutionPolicy Bypass -File `".\God Bot.ps1`" -Action ensure"
Write-Host ""
Write-Host "Docs: docs\GETTING_STARTED.md" -ForegroundColor Green
