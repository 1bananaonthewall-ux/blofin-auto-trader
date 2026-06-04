# One maximized God Bot log tail; close all other PowerShell windows.
$Root = 'C:\Users\mknig\blofin-auto-trader'
$logPath = Join-Path $Root 'logs\bot.log'

function Test-LogWindow {
    param([int]$ProcessId)
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        return ($p.CommandLine -match 'bot\.log')
    } catch {
        return $false
    }
}

$logProc = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'bot\.log|open_log_only\.ps1' } |
    Select-Object -First 1

if ($logProc) {
    $keepId = [int]$logProc.ProcessId
    cmd.exe /c "start `"God Bot Live Log`" /MAX powershell.exe -NoExit -NoProfile -File `"$(Join-Path $PSScriptRoot 'open_log_only.ps1')`"" 2>$null
    Start-Sleep -Seconds 1
    Stop-Process -Id $keepId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    $started = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'open_log_only\.ps1' } |
        Sort-Object ProcessId -Descending |
        Select-Object -First 1
    $keepId = if ($started) { [int]$started.ProcessId } else { $keepId }
} else {
    $logScript = Join-Path $PSScriptRoot 'open_log_only.ps1'
    cmd.exe /c "start `"God Bot Live Log`" /MAX powershell.exe -NoExit -NoProfile -File `"$logScript`""
    Start-Sleep -Seconds 2
    $started = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'open_log_only\.ps1' } |
        Sort-Object ProcessId -Descending |
        Select-Object -First 1
    $keepId = if ($started) { [int]$started.ProcessId } else { 0 }
}

& (Join-Path $PSScriptRoot 'close_other_ps.ps1') -KeepProcessId $keepId

Write-Output "log_window_pid=$keepId"
