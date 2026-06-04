# God Bot full audit (+ optional TPSL repair)
param(
    [switch]$Fix
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
$args = @("scripts\godbot_audit.py")
if ($Fix) { $args += "--fix" }
& $py @args
exit $LASTEXITCODE
