param(
    [switch]$Build,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { $venvPy = "python" }

$storeDir = Join-Path $Root "storefront"
if ($Build -or -not (Test-Path (Join-Path $storeDir "dist\index.html"))) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "Node/npm required to build Bob's Bots storefront. Install Node 20+ or run with -Dev."
    } else {
        Push-Location $storeDir
        if (-not (Test-Path "node_modules")) { npm install }
        npm run build
        Pop-Location
        Write-Host "Bob's Bots storefront built."
    }
}

if ($Dev) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$storeDir'; npm run dev"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$Root'; & '$venvPy' storefront_api.py"
    Write-Host "Dev: UI http://127.0.0.1:5174  API http://127.0.0.1:5070"
    exit 0
}

$port = if ($env:STOREFRONT_PORT) { $env:STOREFRONT_PORT } else { "5070" }
Start-Process -FilePath $venvPy -ArgumentList "storefront_api.py" -WorkingDirectory $Root -WindowStyle Hidden
Start-Sleep -Seconds 2
Write-Host "Bob's Bots live at http://127.0.0.1:$port"
Start-Process "http://127.0.0.1:$port"
