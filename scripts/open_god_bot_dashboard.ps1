# Ensure stack is up, wait for dashboard, open browser.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$BootPs1 = Join-Path $Root "scripts\boot_god_bot_stack.ps1"
$Url = "http://127.0.0.1:5050"
$Port = 5050

& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $BootPs1

for ($i = 0; $i -lt 45; $i++) {
    try {
        if (@(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
            break
        }
    } catch { }
    Start-Sleep -Seconds 1
}

Start-Process $Url
