param([int]$KeepProcessId = 0)
if ($KeepProcessId -le 0) {
    $row = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'bot\.log|open_log_only\.ps1|God Bot Live Log' } |
        Sort-Object { [int]$_.ProcessId } -Descending |
        Select-Object -First 1
    if ($row) { $KeepProcessId = [int]$row.ProcessId }
}
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object {
        $id = [int]$_.ProcessId
        if ($KeepProcessId -gt 0 -and $id -eq $KeepProcessId) { return }
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
            & taskkill.exe /PID $id /F /T 2>$null | Out-Null
        }
    }
