# Persistent repair loop until bot + dashboard are online. Cues Cursor agent via .cursor/STACK_REPAIR_DUE.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "stack_agent_repair.log"
$StateFile = Join-Path $Root "state\stack_repair.json"
$RepairFlag = Join-Path $Root ".cursor\STACK_REPAIR_DUE"
$RepairLock = Join-Path $Root "state\stack_repair.lock"
$Port = 5050

function Write-RepairLog {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $Message" | Out-File $Log -Append -Encoding utf8
}

function Write-RepairState {
    param([hashtable]$Data)
    New-Item -ItemType Directory -Force -Path (Split-Path $StateFile) | Out-Null
    $json = ($Data | ConvertTo-Json -Compress)
    [System.IO.File]::WriteAllText($StateFile, $json, [System.Text.UTF8Encoding]::new($false))
}

function Get-BotLogTail {
    $log = Join-Path $Root "logs\bot.log"
    if (-not (Test-Path $log)) { return "" }
    return (@(Get-Content $log -Tail 25 -ErrorAction SilentlyContinue) -join "`n")
}

function Update-RepairFlag {
    param([int]$Attempt, [string]$Phase, [string]$LastError)
    New-Item -ItemType Directory -Force -Path (Split-Path $RepairFlag) | Out-Null
    @"
triggered_at=$(Get-Date -Format o)
attempt=$Attempt
phase=$Phase
last_error=$LastError
bot_log_tail=$(Get-BotLogTail)
instruction=Bring God Bot stack online (venv bot.py + dashboard :5050). Do not stop until stack_repair.json reports status=done.
"@ | Out-File $RepairFlag -Encoding utf8
}

function Open-CursorIde {
    $paths = @(
        "$env:LOCALAPPDATA\Programs\cursor\Cursor.exe",
        "${env:ProgramFiles}\Cursor\Cursor.exe"
    )
    foreach ($p in $paths) {
        if (Test-Path $p) {
            Start-Process -FilePath $p -ArgumentList "`"$Root`""
            Write-RepairLog "opened Cursor IDE"
            return
        }
    }
    Write-RepairLog "Cursor IDE not found on disk"
}

function Test-StackReady {
    $py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
    try {
        $out = & $py (Join-Path $Root "scripts\stack_repair_check.py") 2>&1 | Out-String
        $j = $out.Trim() | ConvertFrom-Json
        return $j
    } catch {
        return @{ ready = $false; bot_running = $false; dashboard_listening = $false }
    }
}

# Single repair worker.
if (Test-Path $RepairLock) {
    $age = ((Get-Date) - (Get-Item $RepairLock).LastWriteTime).TotalSeconds
    if ($age -lt 7200) {
        Write-RepairLog "repair already running (${age}s) - exit"
        exit 0
    }
    Remove-Item $RepairLock -Force -ErrorAction SilentlyContinue
}
(Get-Date).ToString("o") | Out-File $RepairLock -Encoding ascii

Write-RepairLog "stack_agent_repair start"
Update-RepairFlag -Attempt 0 -Phase "start" -LastError ""
Open-CursorIde

$stackPs1 = Join-Path $Root "scripts\stack_control.ps1"
$dashPs1 = Join-Path $Root "scripts\start_dashboard_quiet.ps1"
$bootPs1 = Join-Path $Root "scripts\boot_god_bot_stack.ps1"
schtasks /Change /TN "BlofinLiveBot" /DISABLE 2>&1 | Out-Null

$attempt = 0
try {
    while ($true) {
        $attempt++
        $phase = if ($attempt % 5 -eq 0) { "restart-fresh" } else { "ensure" }
        Write-RepairLog "attempt $attempt phase=$phase"
        Update-RepairFlag -Attempt $attempt -Phase $phase -LastError ""

        Write-RepairState @{
            status     = "running"
            attempt    = $attempt
            phase      = $phase
            started_at = (Get-Item $RepairLock).LastWriteTime.ToString("o")
            updated_at = (Get-Date).ToString("o")
        }

        if ($phase -eq "restart-fresh") {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $stackPs1 -Action restart-fresh 2>&1 |
                Out-File $Log -Append -Encoding utf8
        } else {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $stackPs1 -Action ensure 2>&1 |
                Out-File $Log -Append -Encoding utf8
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $dashPs1 -Port $Port 2>&1 |
                Out-File $Log -Append -Encoding utf8
        }

        Start-Sleep -Seconds 15
        $check = Test-StackReady
        if ($check.ready) {
            Write-RepairLog "stack ready on attempt $attempt"
            Write-RepairState @{
                status     = "done"
                attempt    = $attempt
                phase      = $phase
                updated_at = (Get-Date).ToString("o")
                bot_running = $check.bot_running
                dashboard_listening = $check.dashboard_listening
            }
            if (Test-Path $RepairFlag) { Remove-Item $RepairFlag -Force -ErrorAction SilentlyContinue }
            break
        }

        $err = "bot=$($check.bot_running) dashboard=$($check.dashboard_listening)"
        Update-RepairFlag -Attempt $attempt -Phase $phase -LastError $err
        Write-RepairState @{
            status     = "running"
            attempt    = $attempt
            phase      = $phase
            updated_at = (Get-Date).ToString("o")
            last_error = $err
            bot_running = $check.bot_running
            dashboard_listening = $check.dashboard_listening
        }
        Start-Sleep -Seconds 30
    }
} finally {
    if (Test-Path $RepairLock) { Remove-Item $RepairLock -Force -ErrorAction SilentlyContinue }
    Write-RepairLog "stack_agent_repair exit"
}
