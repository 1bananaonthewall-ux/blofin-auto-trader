# Register a Windows scheduled task to run the 15-minute optimizer tick.
# Safe to run while bot.py is live — both share state/scalp_tuning.json.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$action = New-ScheduledTaskAction -Execute $python -Argument "scalp_optimizer.py" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "BlofinScalpOptimizer15m" -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Scheduled BlofinScalpOptimizer15m every 15 minutes."
