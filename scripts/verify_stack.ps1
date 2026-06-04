$bots = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*blofin-auto-trader*' -and $_.CommandLine -match 'bot\.py' }
Write-Host "bot_count=$($bots.Count)"
foreach ($b in $bots) {
    Write-Host "bot pid=$($b.ProcessId) venv=$($b.CommandLine -like '*.venv*')"
}
$logs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like '*bot.log*' }
Write-Host "log_windows=$($logs.Count)"
foreach ($l in $logs) { Write-Host "log pid=$($l.ProcessId)" }
Write-Host "powershell_total=$((Get-Process powershell -ea 0 | Measure-Object).Count)"
