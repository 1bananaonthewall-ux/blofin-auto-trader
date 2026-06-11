# God Bot caretaker — runs every ~10 min via Task Scheduler (no Cursor required).
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "god_bot_caretaker.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$ts caretaker tick" | Out-File $Log -Append -Encoding utf8

$Py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $Py scripts\god_bot_caretaker_tick.py 2>&1 | Out-File $Log -Append -Encoding utf8
& $Py scripts\stack_status.py 2>&1 | Out-File $Log -Append -Encoding utf8

# Hourly maintain on the :05 mark (optimizer / ml / throughput) — once per hour max.
$minute = (Get-Date).Minute
if ($minute -ge 4 -and $minute -le 12) {
    $stamp = Join-Path $Root "state\last_caretaker_hourly.txt"
    $runHourly = $true
    if (Test-Path $stamp) {
        try {
            $last = [double](Get-Content $stamp -ErrorAction Stop | Select-Object -First 1)
            if (((Get-Date).ToUniversalTime() - [DateTimeOffset]::FromUnixTimeSeconds([long]$last).UtcDateTime).TotalHours -lt 0.9) {
                $runHourly = $false
            }
        } catch { }
    }
    if ($runHourly -and (Test-Path (Join-Path $Root "scripts\hourly_maintain.py"))) {
        "$ts hourly_maintain via caretaker" | Out-File $Log -Append -Encoding utf8
        & $Py scripts\hourly_maintain.py 2>&1 | Out-File $Log -Append -Encoding utf8
        [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() | Out-File $stamp -Encoding ascii
    }
}
