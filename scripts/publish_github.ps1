# Push blofin-auto-trader to GitHub (private). Requires: gh auth login (once).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Gh = "C:\Program Files\GitHub CLI\gh.exe"
if (-not (Test-Path $Gh)) { $Gh = "gh" }

Set-Location $Root

& $Gh auth status 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "Run once: gh auth login --web -h github.com -p https -s repo"
    exit 1
}

$repoName = "blofin-auto-trader"
$user = (& $Gh api user -q .login).Trim()
Write-Host "GitHub user: $user"

$exists = $false
try { & $Gh repo view "$user/$repoName" 2>$null | Out-Null; $exists = ($LASTEXITCODE -eq 0) } catch {}

if (-not $exists) {
    & $Gh repo create $repoName --private --source=. --remote=origin --description "Blofin 3R scalper — core brain, hourly maintain" --push
} else {
    git remote remove origin 2>$null
    git remote add origin "https://github.com/$user/$repoName.git"
    git push -u origin main
}

Write-Host "Pushed: https://github.com/$user/$repoName"
Write-Host "Next: open https://cursor.com/automations and import .cursor/automations/blofin-hourly-optimize.md"
