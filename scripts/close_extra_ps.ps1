param([int]$KeepPid)
Get-Process -Name powershell -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -ne $KeepPid } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Write-Host "kept=$KeepPid remaining=$((Get-Process powershell -ea 0 | Measure-Object).Count)"
