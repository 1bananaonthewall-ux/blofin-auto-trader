$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action restart
