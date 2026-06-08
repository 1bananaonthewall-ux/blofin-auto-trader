# Detached stop: bot + dashboard (invoked from dashboard Ctrl+F6).
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "stop_stack.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$ts stop_god_bot_stack start" | Out-File $Log -Append -Encoding utf8

schtasks /Change /TN "BlofinStackGuard" /DISABLE 2>&1 | Out-Null

$stackPs1 = Join-Path $Root "scripts\stack_control.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $stackPs1 -Action stop-stack 2>&1 |
    Out-File $Log -Append -Encoding utf8

"$ts stop_god_bot_stack done" | Out-File $Log -Append -Encoding utf8
